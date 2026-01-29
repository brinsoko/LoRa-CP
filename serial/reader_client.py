#!/usr/bin/env python3
import os, sys, time, socket, re, signal
import requests

# ----------------- Config (via env) -----------------
TCP_URL     = os.getenv("SERIAL_URL", "socket://127.0.0.1:2001")
API_URL     = os.getenv("INGEST_URL", "http://127.0.0.1:5001/api/ingest")
COMPETITION_ID = os.getenv("COMPETITION_ID")
TIMEOUT_S   = float(os.getenv("SERIAL_TIMEOUT", "0.2"))      # socket read timeout
BACKOFF_0   = float(os.getenv("BACKOFF_START", "0.5"))       # initial backoff when POST fails / socket drops
BACKOFF_MAX = float(os.getenv("BACKOFF_MAX", "10"))          # max backoff cap
DEBUG       = os.getenv("DEBUG", "0") == "1"
POST_TIMEOUT= float(os.getenv("POST_TIMEOUT", "5"))          # HTTP POST timeout
INGEST_PASSWORD = os.getenv("INGEST_PASSWORD")

# dev_id|payload|rssi|snr   (e.g. 1|A1B2C3D4|-66.0|9.5)
LINE_RE = re.compile(
    r"^\s*(?P<dev>\d+)\|(?P<payload>[^|]+)\|(?P<rssi>-?\d+(?:\.\d+)?)\|(?P<snr>-?\d+(?:\.\d+)?)\s*$"
)

running = True
def _stop(*_):
    global running
    running = False

signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)

# ----------------- Helpers -----------------
def dlog(msg: str):
    if DEBUG:
        print(msg, flush=True)

def open_socket(url: str) -> socket.socket:
    if not url.startswith("socket://"):
        raise ValueError("SERIAL_URL must start with socket://")
    host_port = url[len("socket://"):]
    host, port = host_port.split(":")
    s = socket.create_connection((host, int(port)), timeout=5)
    s.settimeout(TIMEOUT_S)
    return s

def process_buffer(buf: bytes):
    """
    Split buffer into complete lines and remainder.
    Accepts CR, LF, or CRLF. Returns (list_of_lines_bytes, remainder_bytes).
    """
    if not buf:
        return [], b""
    norm = buf.replace(b"\r", b"\n")
    parts = norm.split(b"\n")
    lines = parts[:-1]         # complete lines
    remainder = parts[-1]      # incomplete (may be empty)
    return lines, remainder

# ----------------- Main -----------------
def run():
    print(f"[reader] connecting to {TCP_URL} -> POST {API_URL}", flush=True)
    backoff = BACKOFF_0
    buf = b""

    # reuse HTTP session for keep-alive & fewer allocations
    session = requests.Session()

    while running:
        try:
            sock = open_socket(TCP_URL)
            print("[reader] socket connected", flush=True)
            backoff = BACKOFF_0

            while running:
                try:
                    chunk = sock.recv(1024)
                    if not chunk:
                        raise ConnectionError("socket closed by peer")

                    buf += chunk
                    lines, buf = process_buffer(buf)

                    if DEBUG and lines:
                        dlog(f"[reader] recv {len(chunk)}B -> {len(lines)} line(s), remainder {len(buf)}B")

                    for raw in lines:
                        line = raw.decode("utf-8", "ignore").strip()
                        if not line:
                            continue

                        m = LINE_RE.match(line)
                        if not m:
                            print(f"[reader] skip (bad line): {repr(line)}", flush=True)
                            continue

                        try:
                            dev = int(m.group("dev"))
                            payload = m.group("payload").strip()
                            rssi = float(m.group("rssi"))
                            snr  = float(m.group("snr"))
                        except Exception as e:
                            print(f"[reader] parse error: {e} for line={repr(line)}", flush=True)
                            continue

                        # POST to API
                        try:
                            body = {"dev_id": dev, "payload": payload, "rssi": rssi, "snr": snr}
                            if COMPETITION_ID:
                                try:
                                    body["competition_id"] = int(COMPETITION_ID)
                                except Exception:
                                    body["competition_id"] = COMPETITION_ID
                            if INGEST_PASSWORD:
                                body["ingest_password"] = INGEST_PASSWORD
                            resp = session.post(API_URL, json=body, timeout=POST_TIMEOUT)
                            resp.raise_for_status()
                            print(f"[reader] OK dev={dev} payload={payload} -> {resp.json()}", flush=True)
                            backoff = BACKOFF_0  # reset backoff on success
                        except requests.RequestException as e:
                            print(f"[reader] ingest error: {e}", flush=True)
                            time.sleep(min(backoff, BACKOFF_MAX))
                            backoff = min(backoff * 2, BACKOFF_MAX)

                    # safety: cap buffer growth if no newlines ever arrive
                    if len(buf) > 4096:
                        dlog(f"[reader] trimming buffer (len={len(buf)})")
                        buf = buf[-1024:]

                except socket.timeout:
                    # just idle; loop again
                    continue

        except Exception as e:
            if not running:
                break
            print(f"[reader] socket error: {e} (reconnecting...)", flush=True)
            time.sleep(min(backoff, BACKOFF_MAX))
            backoff = min(backoff * 2, BACKOFF_MAX)

    print("[reader] shutting down", flush=True)

if __name__ == "__main__":
    run()
