#!/usr/bin/env python3
"""
stress_test.py – LoRa-CP Throughput & Database Stress Tester
=============================================================

Tests a live/remote LoRa-CP deployment under realistic and extreme load.
Does NOT require access to the source code – it hits the HTTP API directly.

Usage examples
--------------
# Basic run against a local dev server
python stress_test.py --base-url http://localhost:5000

# Against a remote hosted instance with auth cookie
python stress_test.py --base-url https://my-lora-cp.example.com \\
    --cookie "session=<your-session-cookie>" \\
    --competition-id 1

# Full parametric run
python stress_test.py \\
    --base-url https://my-lora-cp.example.com \\
    --cookie "session=<cookie>" \\
    --competition-id 2 \\
    --workers 20 \\
    --ingest-rps 50 \\
    --duration 60 \\
    --ramp-up 10 \\
    --webhook-secret CHANGE_LATER \\
    --report report.json

Requirements
------------
    pip install requests rich
    # optional (for live chart):
    # pip install plotext
"""

import argparse
import json
import math
import os
import random
import statistics
import string
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Rich for pretty terminal output (gracefully degraded if absent)
try:
    from rich.console import Console
    from rich.table import Table
    from rich.progress import Progress, SpinnerColumn, BarColumn, TimeElapsedColumn
    from rich.live import Live
    from rich.panel import Panel
    from rich import box
    RICH = True
    console = Console()
except ImportError:
    RICH = False
    class _FallbackConsole:
        def print(self, *a, **kw): print(*a)
        def rule(self, *a, **kw): print("─" * 60)
    console = _FallbackConsole()

# Optional live ASCII chart
try:
    import plotext as plt
    PLOTEXT = True
except ImportError:
    PLOTEXT = False


# ---------------------------------------------------------------------------
# Configuration & constants
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL    = "http://127.0.0.1:5001"
DEFAULT_WORKERS     = 10
DEFAULT_INGEST_RPS  = 20       # target requests per second
DEFAULT_DURATION    = 30       # seconds of sustained load
DEFAULT_RAMP_UP     = 5        # seconds to ramp from 0 → target RPS
DEFAULT_COMPETITION = 1
DEFAULT_HMAC_LEN    = 8

INGEST_PATH  = "/api/ingest"
VERIFY_PATH  = "/api/rfid/verify"
CHECKIN_PATH = "/checkins/"
CSV_PATH     = "/checkins/export.csv"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class RequestResult:
    endpoint:    str
    status_code: int
    latency_ms:  float
    success:     bool
    error:       Optional[str] = None
    ts:          float = field(default_factory=time.monotonic)


@dataclass
class ScenarioStats:
    name:          str
    total:         int = 0
    ok:            int = 0
    errors:        int = 0
    latencies:     List[float] = field(default_factory=list)
    status_counts: Dict[int, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def error_rate(self) -> float:
        return (self.errors / self.total * 100) if self.total else 0.0

    @property
    def p50(self) -> float:
        return statistics.median(self.latencies) if self.latencies else 0.0

    @property
    def p95(self) -> float:
        if not self.latencies:
            return 0.0
        s = sorted(self.latencies)
        return s[int(len(s) * 0.95)]

    @property
    def p99(self) -> float:
        if not self.latencies:
            return 0.0
        s = sorted(self.latencies)
        return s[int(len(s) * 0.99)]

    @property
    def mean(self) -> float:
        return statistics.mean(self.latencies) if self.latencies else 0.0

    @property
    def max_lat(self) -> float:
        return max(self.latencies) if self.latencies else 0.0


# ---------------------------------------------------------------------------
# HTTP session factory
# ---------------------------------------------------------------------------

def _make_session(cookie: Optional[str], webhook_secret: Optional[str]) -> requests.Session:
    sess = requests.Session()
    adapter = HTTPAdapter(
        max_retries=Retry(total=0),       # no retries – we want raw failures
        pool_connections=50,
        pool_maxsize=200,
    )
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)
    if cookie:
        # Accept raw "key=value" or just a session token
        if "=" in cookie:
            name, _, val = cookie.partition("=")
            sess.cookies.set(name.strip(), val.strip())
        else:
            sess.cookies.set("session", cookie.strip())
    if webhook_secret:
        sess.headers.update({"X-Webhook-Secret": webhook_secret})
    sess.headers.update({"Content-Type": "application/json"})
    return sess


# ---------------------------------------------------------------------------
# UID / payload generators
# ---------------------------------------------------------------------------

def _random_uid(length: int = 8) -> str:
    return "".join(random.choices("0123456789ABCDEF", k=length))


def _random_dev_id(pool: List[int]) -> int:
    return random.choice(pool) if pool else random.randint(1, 10)


# ---------------------------------------------------------------------------
# Individual scenario workers
# ---------------------------------------------------------------------------

class IngestWorker:
    """Hammers POST /api/ingest at a given rate."""

    def __init__(self, base_url: str, session: requests.Session,
                 competition_id: int, dev_id_pool: List[int],
                 uid_pool: List[str], results: List[RequestResult],
                 lock: threading.Lock, stop_event: threading.Event,
                 target_interval: float):
        self.base_url        = base_url.rstrip("/")
        self.session         = session
        self.competition_id  = competition_id
        self.dev_id_pool     = dev_id_pool
        self.uid_pool        = uid_pool
        self.results         = results
        self.lock            = lock
        self.stop            = stop_event
        self.target_interval = target_interval  # seconds between requests

    def run(self):
        url = self.base_url + INGEST_PATH
        while not self.stop.is_set():
            uid    = random.choice(self.uid_pool)
            dev_id = _random_dev_id(self.dev_id_pool)
            payload = {
                "competition_id": self.competition_id,
                "dev_id":         dev_id,
                "payload":        uid,
                "rssi":           round(random.uniform(-120, -40), 1),
                "snr":            round(random.uniform(-5, 15), 1),
            }
            t0 = time.monotonic()
            try:
                resp = self.session.post(url, json=payload, timeout=10)
                lat  = (time.monotonic() - t0) * 1000
                ok   = resp.status_code in (200, 201)
                r    = RequestResult(INGEST_PATH, resp.status_code, lat, ok)
            except requests.exceptions.Timeout:
                lat = (time.monotonic() - t0) * 1000
                r   = RequestResult(INGEST_PATH, 0, lat, False, "Timeout")
            except Exception as exc:
                lat = (time.monotonic() - t0) * 1000
                r   = RequestResult(INGEST_PATH, 0, lat, False, str(exc))

            with self.lock:
                self.results.append(r)

            # Rate limiter: sleep any remaining time in the interval
            elapsed = time.monotonic() - t0
            sleep_s = self.target_interval - elapsed
            if sleep_s > 0:
                time.sleep(sleep_s)


class ReadWorker:
    """Reads /checkins/ and /checkins/export.csv to simulate judge/viewer load."""

    def __init__(self, base_url: str, session: requests.Session,
                 results: List[RequestResult], lock: threading.Lock,
                 stop_event: threading.Event, interval: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self.session  = session
        self.results  = results
        self.lock     = lock
        self.stop     = stop_event
        self.interval = interval

    def run(self):
        endpoints = [CHECKIN_PATH, CSV_PATH + "?sort=new"]
        while not self.stop.is_set():
            ep  = random.choice(endpoints)
            url = self.base_url + ep
            t0  = time.monotonic()
            try:
                resp = self.session.get(url, timeout=15)
                lat  = (time.monotonic() - t0) * 1000
                ok   = resp.status_code in (200, 302)
                r    = RequestResult(ep, resp.status_code, lat, ok)
            except Exception as exc:
                lat = (time.monotonic() - t0) * 1000
                r   = RequestResult(ep, 0, lat, False, str(exc))

            with self.lock:
                self.results.append(r)
            time.sleep(self.interval)


class VerifyWorker:
    """Exercises POST /api/rfid/verify."""

    def __init__(self, base_url: str, session: requests.Session,
                 competition_id: int, dev_id_pool: List[int],
                 uid_pool: List[str], results: List[RequestResult],
                 lock: threading.Lock, stop_event: threading.Event,
                 interval: float = 1.0):
        self.base_url       = base_url.rstrip("/")
        self.session        = session
        self.competition_id = competition_id
        self.dev_id_pool    = dev_id_pool
        self.uid_pool       = uid_pool
        self.results        = results
        self.lock           = lock
        self.stop           = stop_event
        self.interval       = interval

    def run(self):
        import hashlib, hmac as _hmac
        secret = "card-secret"   # default – override via CLI if different
        url    = self.base_url + VERIFY_PATH
        while not self.stop.is_set():
            uid     = random.choice(self.uid_pool)
            dev_ids = random.sample(self.dev_id_pool, min(3, len(self.dev_id_pool)))
            digests = []
            for d in dev_ids:
                raw = _hmac.new(secret.encode(),
                                f"{d}|{uid}".encode(),
                                hashlib.sha256).hexdigest()
                digests.append(raw[:DEFAULT_HMAC_LEN])

            body = {"uid": uid, "digests": digests, "device_ids": dev_ids}
            t0   = time.monotonic()
            try:
                resp = self.session.post(url, json=body, timeout=10)
                lat  = (time.monotonic() - t0) * 1000
                ok   = resp.status_code == 200
                r    = RequestResult(VERIFY_PATH, resp.status_code, lat, ok)
            except Exception as exc:
                lat = (time.monotonic() - t0) * 1000
                r   = RequestResult(VERIFY_PATH, 0, lat, False, str(exc))

            with self.lock:
                self.results.append(r)
            time.sleep(self.interval)


# ---------------------------------------------------------------------------
# Ramp controller
# ---------------------------------------------------------------------------

class RampController:
    """Linearly ramps active worker count from 0 → max over ramp_seconds."""

    def __init__(self, workers: List[threading.Thread],
                 ramp_seconds: float, start_delay: float = 0.1):
        self.workers      = workers
        self.ramp_seconds = ramp_seconds
        self.start_delay  = start_delay

    def start(self):
        n = len(self.workers)
        if n == 0:
            return
        interval = self.ramp_seconds / n
        for w in self.workers:
            w.start()
            time.sleep(max(interval, self.start_delay))


# ---------------------------------------------------------------------------
# Live metrics ticker
# ---------------------------------------------------------------------------

class MetricsTicker:
    """Prints a one-line rolling summary every second."""

    def __init__(self, results: List[RequestResult],
                 lock: threading.Lock, stop_event: threading.Event):
        self.results = results
        self.lock    = lock
        self.stop    = stop_event
        self._prev_total = 0
        self._start      = time.monotonic()

    def run(self):
        while not self.stop.is_set():
            time.sleep(1.0)
            with self.lock:
                total = len(self.results)
                recent = [r for r in self.results[-200:]
                          if (time.monotonic() - r.ts) < 5.0]

            rps      = total - self._prev_total
            self._prev_total = total
            ok       = sum(1 for r in recent if r.success)
            err      = len(recent) - ok
            lats     = [r.latency_ms for r in recent]
            p50      = f"{statistics.median(lats):.0f}" if lats else "—"
            p95_val  = sorted(lats)[int(len(lats)*0.95)] if lats else 0
            elapsed  = time.monotonic() - self._start

            line = (f"  t={elapsed:5.0f}s │ rps={rps:4d} │ total={total:6d} │ "
                    f"ok={ok:4d} err={err:3d} │ p50={p50}ms p95={p95_val:.0f}ms")
            console.print(line)


# ---------------------------------------------------------------------------
# Aggregation & reporting
# ---------------------------------------------------------------------------

def _aggregate(results: List[RequestResult]) -> Dict[str, ScenarioStats]:
    stats: Dict[str, ScenarioStats] = {}
    for r in results:
        if r.endpoint not in stats:
            stats[r.endpoint] = ScenarioStats(name=r.endpoint)
        s = stats[r.endpoint]
        s.total += 1
        s.latencies.append(r.latency_ms)
        s.status_counts[r.status_code] += 1
        if r.success:
            s.ok += 1
        else:
            s.errors += 1
    return stats


def _print_report(stats: Dict[str, ScenarioStats],
                  duration: float, workers: int,
                  target_rps: int):

    if RICH:
        console.rule("[bold cyan]Stress Test Results")
        table = Table(box=box.ROUNDED, show_header=True,
                      header_style="bold magenta")
        table.add_column("Endpoint",    style="cyan",    no_wrap=True)
        table.add_column("Total",       justify="right")
        table.add_column("OK",          justify="right", style="green")
        table.add_column("Errors",      justify="right", style="red")
        table.add_column("Err %",       justify="right")
        table.add_column("Mean ms",     justify="right")
        table.add_column("p50 ms",      justify="right")
        table.add_column("p95 ms",      justify="right")
        table.add_column("p99 ms",      justify="right")
        table.add_column("Max ms",      justify="right")
        table.add_column("Actual RPS",  justify="right")

        for ep, s in sorted(stats.items()):
            actual_rps = f"{s.total / duration:.1f}" if duration else "—"
            err_style  = "red" if s.error_rate > 5 else "yellow" if s.error_rate > 1 else "green"
            table.add_row(
                ep,
                str(s.total),
                str(s.ok),
                str(s.errors),
                f"[{err_style}]{s.error_rate:.1f}%[/{err_style}]",
                f"{s.mean:.1f}",
                f"{s.p50:.1f}",
                f"{s.p95:.1f}",
                f"{s.p99:.1f}",
                f"{s.max_lat:.1f}",
                actual_rps,
            )
        console.print(table)

        # Status code breakdown
        console.rule("[bold]Status Code Breakdown")
        for ep, s in sorted(stats.items()):
            codes = ", ".join(f"{k}×{v}" for k, v in sorted(s.status_counts.items()))
            console.print(f"  {ep:40s}  {codes}")

        # Summary verdict
        console.rule()
        total_req = sum(s.total for s in stats.values())
        total_err = sum(s.errors for s in stats.values())
        overall_err_pct = total_err / total_req * 100 if total_req else 0
        achieved_rps    = total_req / duration if duration else 0

        if overall_err_pct < 1.0 and achieved_rps >= target_rps * 0.9:
            verdict = "[bold green]✔  PASS – System handled load within thresholds.[/bold green]"
        elif overall_err_pct < 5.0:
            verdict = "[bold yellow]⚠  MARGINAL – Error rate acceptable but watch latency.[/bold yellow]"
        else:
            verdict = "[bold red]✘  FAIL – Error rate too high or throughput unmet.[/bold red]"

        console.print(Panel(
            f"Total requests : {total_req}\n"
            f"Total errors   : {total_err}  ({overall_err_pct:.2f}%)\n"
            f"Achieved RPS   : {achieved_rps:.1f}  (target {target_rps})\n"
            f"Test duration  : {duration:.1f}s  |  Workers: {workers}\n\n"
            + verdict,
            title="[bold]Summary", expand=False
        ))
    else:
        # Plain fallback
        print("\n" + "="*70)
        print("STRESS TEST RESULTS")
        print("="*70)
        for ep, s in sorted(stats.items()):
            actual_rps = f"{s.total / duration:.1f}" if duration else "—"
            print(f"\n{ep}")
            print(f"  total={s.total}  ok={s.ok}  errors={s.errors}  "
                  f"err%={s.error_rate:.1f}")
            print(f"  mean={s.mean:.1f}ms  p50={s.p50:.1f}ms  "
                  f"p95={s.p95:.1f}ms  p99={s.p99:.1f}ms  max={s.max_lat:.1f}ms")
            print(f"  actual_rps={actual_rps}  statuses={dict(s.status_counts)}")


def _draw_latency_chart(results: List[RequestResult]):
    if not PLOTEXT:
        return
    ingest = [r.latency_ms for r in results if r.endpoint == INGEST_PATH]
    if len(ingest) < 10:
        return
    # Bucket into 1-second windows
    start = results[0].ts if results else 0
    buckets: Dict[int, List[float]] = defaultdict(list)
    for r in results:
        if r.endpoint == INGEST_PATH:
            bucket = int(r.ts - start)
            buckets[bucket].append(r.latency_ms)
    xs = sorted(buckets)
    ys = [statistics.median(buckets[x]) for x in xs]
    plt.clf()
    plt.plot(xs, ys, label="ingest p50 latency (ms)")
    plt.title("Ingest Latency over Time")
    plt.xlabel("seconds")
    plt.ylabel("ms")
    plt.show()


def _save_report(stats: Dict[str, ScenarioStats],
                 results: List[RequestResult],
                 args, path: str):
    report = {
        "meta": {
            "base_url":     args.base_url,
            "competition":  args.competition_id,
            "workers":      args.workers,
            "target_rps":   args.ingest_rps,
            "duration_s":   args.duration,
            "ramp_up_s":    args.ramp_up,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "scenarios": {},
    }
    for ep, s in stats.items():
        report["scenarios"][ep] = {
            "total":   s.total,
            "ok":      s.ok,
            "errors":  s.errors,
            "error_pct": round(s.error_rate, 3),
            "latency": {
                "mean": round(s.mean, 2),
                "p50":  round(s.p50, 2),
                "p95":  round(s.p95, 2),
                "p99":  round(s.p99, 2),
                "max":  round(s.max_lat, 2),
            },
            "status_codes": dict(s.status_counts),
            "actual_rps":   round(s.total / args.duration, 2) if args.duration else 0,
        }

    # Timeline sample (1 row per second, ingest only)
    start = results[0].ts if results else 0
    timeline: Dict[int, Dict] = {}
    for r in results:
        if r.endpoint != INGEST_PATH:
            continue
        bucket = int(r.ts - start)
        if bucket not in timeline:
            timeline[bucket] = {"count": 0, "errors": 0, "lats": []}
        timeline[bucket]["count"] += 1
        if not r.success:
            timeline[bucket]["errors"] += 1
        timeline[bucket]["lats"].append(r.latency_ms)

    report["timeline"] = {
        str(k): {
            "rps":    v["count"],
            "errors": v["errors"],
            "p50_ms": round(statistics.median(v["lats"]), 1) if v["lats"] else 0,
        }
        for k, v in sorted(timeline.items())
    }

    with open(path, "w") as f:
        json.dump(report, f, indent=2)
    console.print(f"\n  Report saved → [bold]{path}[/bold]")


# ---------------------------------------------------------------------------
# Pre-flight connectivity check
# ---------------------------------------------------------------------------

def _preflight(base_url: str, session: requests.Session) -> bool:
    console.print(f"\n  Checking connectivity to [bold]{base_url}[/bold] …")
    try:
        r = session.get(base_url + "/", timeout=8, allow_redirects=True)
        console.print(f"  HTTP {r.status_code} – server reachable ✔")
        return True
    except requests.exceptions.ConnectionError:
        console.print(f"  [red]✘ Cannot connect to {base_url}[/red]")
        return False
    except requests.exceptions.Timeout:
        console.print("  [red]✘ Connection timed out[/red]")
        return False


# ---------------------------------------------------------------------------
# Seed data helpers (optional – prepopulate fake devices/UIDs when no real data)
# ---------------------------------------------------------------------------

def _seed_uid_pool(n: int = 200) -> List[str]:
    """Generate a pool of plausible RFID UIDs."""
    return [_random_uid(8) for _ in range(n)]


def _seed_dev_id_pool(start: int = 1, count: int = 5) -> List[int]:
    return list(range(start, start + count))


# ---------------------------------------------------------------------------
# Main test runner
# ---------------------------------------------------------------------------

def run_stress_test(args):
    console.print("\n")
    if RICH:
        console.rule("[bold cyan]LoRa-CP Stress Test")
    else:
        console.print("=" * 60 + "\nLoRa-CP Stress Test\n" + "=" * 60)

    session = _make_session(args.cookie, args.webhook_secret)

    if not _preflight(args.base_url, session):
        sys.exit(1)

    # ── Pools ──────────────────────────────────────────────────────────────
    uid_pool    = _seed_uid_pool(args.uid_pool_size)
    dev_id_pool = _seed_dev_id_pool(args.dev_id_start, args.dev_id_count)

    console.print(f"\n  UIDs in pool    : {len(uid_pool)}")
    console.print(f"  Device IDs      : {dev_id_pool}")
    console.print(f"  Competition ID  : {args.competition_id}")
    console.print(f"  Target RPS      : {args.ingest_rps}")
    console.print(f"  Workers         : {args.workers}")
    console.print(f"  Duration        : {args.duration}s  (ramp {args.ramp_up}s)\n")

    results: List[RequestResult] = []
    lock        = threading.Lock()
    stop_event  = threading.Event()

    # ── Worker interval per worker ─────────────────────────────────────────
    # Each ingest worker fires one request then sleeps.
    # interval = workers / target_rps  → combined rate ≈ target_rps
    ingest_interval = args.workers / args.ingest_rps if args.ingest_rps else 0.05

    # ── Build worker threads ───────────────────────────────────────────────
    ingest_threads = []
    for i in range(args.workers):
        # Each worker gets its own session to avoid lock contention
        w_session = _make_session(args.cookie, args.webhook_secret)
        worker = IngestWorker(
            base_url=args.base_url,
            session=w_session,
            competition_id=args.competition_id,
            dev_id_pool=dev_id_pool,
            uid_pool=uid_pool,
            results=results,
            lock=lock,
            stop_event=stop_event,
            target_interval=ingest_interval,
        )
        t = threading.Thread(target=worker.run, daemon=True,
                             name=f"ingest-{i}")
        ingest_threads.append(t)

    # Read workers (viewer/judge simulation)
    read_threads = []
    if args.read_workers > 0:
        for i in range(args.read_workers):
            w_session = _make_session(args.cookie, args.webhook_secret)
            worker = ReadWorker(
                base_url=args.base_url,
                session=w_session,
                results=results,
                lock=lock,
                stop_event=stop_event,
                interval=max(1.0, args.workers / 5),
            )
            t = threading.Thread(target=worker.run, daemon=True,
                                 name=f"read-{i}")
            read_threads.append(t)

    # Verify workers
    verify_threads = []
    if args.verify_workers > 0:
        for i in range(args.verify_workers):
            w_session = _make_session(args.cookie, args.webhook_secret)
            worker = VerifyWorker(
                base_url=args.base_url,
                session=w_session,
                competition_id=args.competition_id,
                dev_id_pool=dev_id_pool,
                uid_pool=uid_pool,
                results=results,
                lock=lock,
                stop_event=stop_event,
                interval=1.5,
            )
            t = threading.Thread(target=worker.run, daemon=True,
                                 name=f"verify-{i}")
            verify_threads.append(t)

    # Metrics ticker
    ticker = MetricsTicker(results, lock, stop_event)
    ticker_thread = threading.Thread(target=ticker.run, daemon=True,
                                     name="ticker")

    # ── Start ──────────────────────────────────────────────────────────────
    console.print(f"  [bold]Ramping up over {args.ramp_up}s …[/bold]")
    ticker_thread.start()
    ramp = RampController(ingest_threads, args.ramp_up)
    ramp.start()
    for t in read_threads + verify_threads:
        t.start()

    console.print(f"  [bold]Sustaining load for {args.duration}s …[/bold]\n")
    test_start = time.monotonic()

    try:
        while time.monotonic() - test_start < (args.ramp_up + args.duration):
            time.sleep(0.5)
    except KeyboardInterrupt:
        console.print("\n  [yellow]Interrupted by user.[/yellow]")

    # ── Stop ───────────────────────────────────────────────────────────────
    stop_event.set()
    all_threads = ingest_threads + read_threads + verify_threads + [ticker_thread]
    for t in all_threads:
        t.join(timeout=5)

    actual_duration = time.monotonic() - test_start

    # ── Report ─────────────────────────────────────────────────────────────
    with lock:
        snapshot = list(results)

    stats = _aggregate(snapshot)
    _print_report(stats, actual_duration, args.workers, args.ingest_rps)

    if PLOTEXT:
        _draw_latency_chart(snapshot)

    if args.report:
        _save_report(stats, snapshot, args, args.report)

    # ── Thresholds check ───────────────────────────────────────────────────
    if INGEST_PATH in stats:
        s = stats[INGEST_PATH]
        violations = []
        if s.error_rate > args.max_error_pct:
            violations.append(
                f"Error rate {s.error_rate:.1f}% > threshold {args.max_error_pct}%")
        if s.p95 > args.max_p95_ms:
            violations.append(
                f"p95 latency {s.p95:.1f}ms > threshold {args.max_p95_ms}ms")
        actual_rps = s.total / actual_duration if actual_duration else 0
        if actual_rps < args.ingest_rps * 0.8:
            violations.append(
                f"Achieved RPS {actual_rps:.1f} < 80% of target {args.ingest_rps}")
        if violations:
            console.print("\n  [bold red]THRESHOLD VIOLATIONS:[/bold red]")
            for v in violations:
                console.print(f"    ✘ {v}")
            sys.exit(2)
        else:
            console.print("\n  [bold green]All thresholds met.[/bold green]")


# ---------------------------------------------------------------------------
# Spike test: burst of requests in a short window
# ---------------------------------------------------------------------------

def run_spike_test(args):
    console.print("\n")
    if RICH:
        console.rule("[bold red]Spike Test  (10× normal RPS for 5 seconds)")
    else:
        console.print("Spike Test")

    session    = _make_session(args.cookie, args.webhook_secret)
    uid_pool   = _seed_uid_pool(50)
    dev_ids    = _seed_dev_id_pool(args.dev_id_start, args.dev_id_count)
    results    = []
    lock       = threading.Lock()
    stop_event = threading.Event()
    url        = args.base_url.rstrip("/") + INGEST_PATH

    spike_workers = args.workers * 3
    spike_rps     = args.ingest_rps * 10
    interval      = spike_workers / spike_rps if spike_rps else 0.01

    threads = []
    for i in range(spike_workers):
        w_sess = _make_session(args.cookie, args.webhook_secret)
        worker = IngestWorker(args.base_url, w_sess, args.competition_id,
                              dev_ids, uid_pool, results, lock, stop_event,
                              interval)
        t = threading.Thread(target=worker.run, daemon=True)
        threads.append(t)
        t.start()

    time.sleep(5)
    stop_event.set()
    for t in threads:
        t.join(timeout=5)

    stats = _aggregate(results)
    _print_report(stats, 5.0, spike_workers, spike_rps)


# ---------------------------------------------------------------------------
# Endurance test: sustained low-level load for a long time
# ---------------------------------------------------------------------------

def run_endurance_test(args, duration_override: int = 300):
    original   = args.duration
    args.duration = duration_override
    args.workers  = max(2, args.workers // 2)
    args.ingest_rps = max(5, args.ingest_rps // 2)
    console.print(f"\n  Endurance test: {duration_override}s "
                  f"at {args.ingest_rps} RPS with {args.workers} workers")
    run_stress_test(args)
    args.duration = original


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser():
    p = argparse.ArgumentParser(
        description="LoRa-CP HTTP/database throughput stress tester",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--base-url",       default=DEFAULT_BASE_URL,
                   help="Base URL of the running LoRa-CP instance")
    p.add_argument("--cookie",         default=None,
                   help="Session cookie  e.g. 'session=<token>'")
    p.add_argument("--webhook-secret", default=None,
                   help="Value for X-Webhook-Secret header")
    p.add_argument("--competition-id", type=int, default=DEFAULT_COMPETITION,
                   help="Competition ID to target")
    p.add_argument("--workers",        type=int, default=DEFAULT_WORKERS,
                   help="Number of concurrent ingest threads")
    p.add_argument("--ingest-rps",     type=int, default=DEFAULT_INGEST_RPS,
                   help="Target ingest requests per second")
    p.add_argument("--duration",       type=int, default=DEFAULT_DURATION,
                   help="Sustained load duration (seconds, after ramp-up)")
    p.add_argument("--ramp-up",        type=int, default=DEFAULT_RAMP_UP,
                   help="Ramp-up period (seconds)")
    p.add_argument("--read-workers",   type=int, default=2,
                   help="Threads reading /checkins/ (viewer simulation)")
    p.add_argument("--verify-workers", type=int, default=1,
                   help="Threads calling /api/rfid/verify")
    p.add_argument("--uid-pool-size",  type=int, default=200,
                   help="Number of distinct UIDs in the test pool")
    p.add_argument("--dev-id-start",   type=int, default=1,
                   help="Lowest device ID to use")
    p.add_argument("--dev-id-count",   type=int, default=5,
                   help="Number of device IDs to cycle through")
    p.add_argument("--max-error-pct",  type=float, default=5.0,
                   help="Fail if error rate exceeds this %%")
    p.add_argument("--max-p95-ms",     type=float, default=2000.0,
                   help="Fail if ingest p95 latency exceeds this ms")
    p.add_argument("--scenario",
                   choices=["load", "spike", "endurance", "all"],
                   default="load",
                   help="Which scenario(s) to run")
    p.add_argument("--report",         default=None,
                   help="Path to write JSON report  e.g. report.json")
    return p


def main():
    parser = _build_parser()
    args   = parser.parse_args()

    if args.scenario in ("load", "all"):
        run_stress_test(args)

    if args.scenario in ("spike", "all"):
        run_spike_test(args)

    if args.scenario in ("endurance", "all"):
        run_endurance_test(args, duration_override=120)


if __name__ == "__main__":
    main()
