from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterable
from typing import Any

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError
from gspread.utils import rowcol_to_a1

from app.utils.export_safety import escape_formula_cell, escape_formula_cells

log = logging.getLogger(__name__)


_RATE_LIMIT_SLEEP_S = 65
_MAX_ATTEMPTS = 3
_QUOTA_TOKENS = ("429", "Quota exceeded", "rateLimitExceeded")


def _is_quota_error(exc: APIError) -> bool:
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None) or getattr(resp, "status", None)
    if status == 429:
        return True
    text = str(exc)
    return any(token in text for token in _QUOTA_TOKENS)


class SheetsClient:
    def __init__(
        self,
        service_account_file: str | None = None,
        service_account_json: str | None = None,
        scopes: Iterable[str] | None = None,
    ):
        scopes = scopes or [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        if not service_account_file and not service_account_json:
            raise RuntimeError("SheetsClient requires a service account file or JSON string")

        if service_account_json:
            info = json.loads(service_account_json)
            creds = Credentials.from_service_account_info(info, scopes=scopes)
        else:
            creds = Credentials.from_service_account_file(service_account_file, scopes=scopes)

        self.gc = gspread.authorize(creds)
        self._lock = threading.Lock()
        self._call_window_start = time.monotonic()
        self._call_count = 0

    def _throttle(self) -> None:
        """Throttle after ~40 calls per minute to dodge Sheets read quotas.

        Thread-safe: every caller mutates the shared counter under a lock so
        the 40/60s window is enforced cumulatively across worker threads
        sharing the singleton client.
        """
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._call_window_start
            if elapsed >= 60:
                self._call_window_start = now
                self._call_count = 0
                elapsed = 0
            self._call_count += 1
            if self._call_count > 40 and elapsed < 60:
                wait_for = 60 - elapsed
                log.info("SheetsClient throttling for %.1fs to avoid API limits", wait_for)
                # Sleep while holding the lock so other threads queue up behind
                # us instead of racing through; the throttle window is short.
                time.sleep(wait_for)
                self._call_window_start = time.monotonic()
                self._call_count = 0

    def _call(self, fn, *args, **kwargs) -> Any:
        """Route every Google API call through here.

        Throttles, then retries up to _MAX_ATTEMPTS on quota (429) errors,
        sleeping _RATE_LIMIT_SLEEP_S between attempts. Non-quota APIErrors
        and unrelated exceptions propagate immediately.
        """
        last_exc: APIError | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            self._throttle()
            try:
                return fn(*args, **kwargs)
            except APIError as exc:
                if not _is_quota_error(exc):
                    raise
                last_exc = exc
                if attempt >= _MAX_ATTEMPTS:
                    break
                log.warning(
                    "SheetsClient quota hit (attempt %d/%d), sleeping %ds",
                    attempt,
                    _MAX_ATTEMPTS,
                    _RATE_LIMIT_SLEEP_S,
                )
                time.sleep(_RATE_LIMIT_SLEEP_S)
        assert last_exc is not None
        raise last_exc

    # Spreadsheet helpers
    def create_spreadsheet(self, title: str, initial_tabs: list[str] | None = None) -> gspread.Spreadsheet:
        ss = self._call(self.gc.create, title)
        if initial_tabs:
            default_sheet = ss.sheet1
            self._call(default_sheet.update_title, initial_tabs[0])
            for tab in initial_tabs[1:]:
                self._call(ss.add_worksheet, title=tab, rows=100, cols=26)
        return ss

    def add_tab(self, spreadsheet_id: str, title: str, rows: int = 100, cols: int = 26) -> gspread.Worksheet:
        ss = self._call(self.gc.open_by_key, spreadsheet_id)
        return self._call(ss.add_worksheet, title=title, rows=rows, cols=cols)

    def set_header_row(self, spreadsheet_id: str, tab_name: str, headers: list[str]) -> gspread.Worksheet:
        ss = self._call(self.gc.open_by_key, spreadsheet_id)
        ws = self._call(ss.worksheet, tab_name)
        self._call(
            ws.update,
            range_name=f"A1:{rowcol_to_a1(1, len(headers))}",
            values=[escape_formula_cells(headers)],
            value_input_option="USER_ENTERED",
        )
        return ws

    def update_column(
        self,
        spreadsheet_id: str,
        tab_name: str,
        col_index: int,
        start_row: int,
        values: list,
    ) -> None:
        """Write a single column (vertical) starting at start_row.

        Strings are escaped so user-supplied values that begin with =/+/-/@
        land as literal text. value_input_option is USER_ENTERED so that
        numbers and dates are still parsed by Sheets.
        """
        ss = self._call(self.gc.open_by_key, spreadsheet_id)
        ws = self._call(ss.worksheet, tab_name)
        end_row = start_row + len(values) - 1
        rng = f"{rowcol_to_a1(start_row, col_index)}:{rowcol_to_a1(end_row, col_index)}"
        self._call(
            ws.update,
            range_name=rng,
            values=[[escape_formula_cell(v)] for v in values],
            value_input_option="USER_ENTERED",
        )

    def batch_update_columns(
        self,
        spreadsheet_id: str,
        tab_name: str,
        columns: list[dict],
    ) -> None:
        """Write multiple column ranges in a single Sheets API call.

        `columns` is a list of dicts like:
          [{"col": 1, "start_row": 2, "values": [101, 102, 103]},
           {"col": 6, "start_row": 2, "values": [201, 202]}]

        Each entry becomes a range update bundled into one ws.batch_update
        request. String values are escape_formula_cell'd; everything else
        (numbers, formulas the caller has authored) passes through.

        Used by sync_all_checkpoint_tabs to refresh team numbers across
        every group block on a CP tab in one call instead of N — the
        prior per-group pattern hit the 40-calls/60s throttle on big
        competitions and timed out the gunicorn worker.
        """
        if not columns:
            return
        ss = self._call(self.gc.open_by_key, spreadsheet_id)
        ws = self._call(ss.worksheet, tab_name)
        data = []
        for col_spec in columns:
            col = col_spec["col"]
            start_row = col_spec["start_row"]
            values = col_spec["values"]
            if not values:
                continue
            end_row = start_row + len(values) - 1
            rng = f"{rowcol_to_a1(start_row, col)}:{rowcol_to_a1(end_row, col)}"
            data.append(
                {
                    "range": rng,
                    "values": [[escape_formula_cell(v) if isinstance(v, str) else v] for v in values],
                }
            )
        if not data:
            return
        self._call(ws.batch_update, data, value_input_option="USER_ENTERED")

    def update_cell(self, spreadsheet_id: str, tab_name: str, row: int, col: int, value) -> None:
        ss = self._call(self.gc.open_by_key, spreadsheet_id)
        ws = self._call(ss.worksheet, tab_name)
        rng = rowcol_to_a1(row, col)
        self._call(
            ws.update,
            range_name=rng,
            values=[[escape_formula_cell(value)]],
            value_input_option="USER_ENTERED",
        )

    def update_cell_formula(
        self,
        spreadsheet_id: str,
        tab_name: str,
        row: int,
        col: int,
        formula: str,
    ) -> None:
        """Write a system-generated formula at a single cell.

        Caller is responsible for the formula being trusted. ValueError if
        the value does not start with '=' so we never accidentally route
        user input through this path.
        """
        if not isinstance(formula, str) or not formula.startswith("="):
            raise ValueError("update_cell_formula requires a string starting with '='")
        ss = self._call(self.gc.open_by_key, spreadsheet_id)
        ws = self._call(ss.worksheet, tab_name)
        rng = rowcol_to_a1(row, col)
        self._call(
            ws.update,
            range_name=rng,
            values=[[formula]],
            value_input_option="USER_ENTERED",
        )

    def update_column_formula(
        self,
        spreadsheet_id: str,
        tab_name: str,
        col_index: int,
        start_row: int,
        formulas: list[str],
    ) -> None:
        """Write a column of system-generated formulas.

        Every entry must start with '='; ValueError otherwise.
        """
        for f in formulas:
            if not isinstance(f, str) or not f.startswith("="):
                raise ValueError("update_column_formula requires every entry to start with '='")
        ss = self._call(self.gc.open_by_key, spreadsheet_id)
        ws = self._call(ss.worksheet, tab_name)
        end_row = start_row + len(formulas) - 1
        rng = f"{rowcol_to_a1(start_row, col_index)}:{rowcol_to_a1(end_row, col_index)}"
        self._call(
            ws.update,
            range_name=rng,
            values=[[f] for f in formulas],
            value_input_option="USER_ENTERED",
        )

    def set_checkbox_validation(
        self,
        spreadsheet_id: str,
        tab_name: str,
        col_index: int,
        start_row: int,
        end_row: int,
    ) -> None:
        """Apply checkbox validation to a column range."""
        ss = self._call(self.gc.open_by_key, spreadsheet_id)
        ws = self._call(ss.worksheet, tab_name)
        body = {
            "requests": [
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": ws.id,
                            "startRowIndex": start_row - 1,
                            "endRowIndex": end_row,
                            "startColumnIndex": col_index - 1,
                            "endColumnIndex": col_index,
                        },
                        "rule": {
                            "condition": {"type": "BOOLEAN"},
                            "showCustomUi": True,
                        },
                    }
                }
            ]
        }
        self._call(ss.batch_update, body)


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_singleton_lock = threading.Lock()


def get_sheets_client(app) -> SheetsClient:
    """Return a process-wide SheetsClient cached on the Flask app.

    The throttle counter and retry state live on the client instance, so a
    cached singleton lets background-worker threads share one rate budget
    instead of each call site rebuilding a fresh client that bypasses the
    quota window.
    """
    extensions = app.extensions
    existing = extensions.get("sheets_client")
    if existing is not None:
        return existing
    with _singleton_lock:
        existing = extensions.get("sheets_client")
        if existing is not None:
            return existing
        client = SheetsClient(
            service_account_file=app.config.get("GOOGLE_SERVICE_ACCOUNT_FILE"),
            service_account_json=app.config.get("GOOGLE_SERVICE_ACCOUNT_JSON"),
        )
        extensions["sheets_client"] = client
        return client
