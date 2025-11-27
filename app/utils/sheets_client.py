from __future__ import annotations

import json
import logging
from typing import Iterable, List, Optional
import time

import gspread
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials

log = logging.getLogger(__name__)


class SheetsClient:
    def __init__(
        self,
        service_account_file: Optional[str] = None,
        service_account_json: Optional[str] = None,
        scopes: Optional[Iterable[str]] = None,
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
        # simple in-process rate limiter to avoid 429s
        self._call_window_start = time.monotonic()
        self._call_count = 0

    def _throttle(self):
        """Throttle after ~40 calls per minute to dodge Sheets read quotas."""
        now = time.monotonic()
        elapsed = now - self._call_window_start
        if elapsed >= 60:
            self._call_window_start = now
            self._call_count = 0
        self._call_count += 1
        if self._call_count > 40 and elapsed < 60:
            wait_for = 60 - elapsed
            log.info("SheetsClient throttling for %.1fs to avoid API limits", wait_for)
            time.sleep(wait_for)
            self._call_window_start = time.monotonic()
            self._call_count = 0

    # Spreadsheet helpers
    def create_spreadsheet(self, title: str, initial_tabs: Optional[List[str]] = None) -> gspread.Spreadsheet:
        self._throttle()
        ss = self.gc.create(title)
        if initial_tabs:
            # gspread creates a default sheet; rename or replace
            default_sheet = ss.sheet1
            default_sheet.update_title(initial_tabs[0])
            for tab in initial_tabs[1:]:
                ss.add_worksheet(title=tab, rows=100, cols=26)
        return ss

    def add_tab(self, spreadsheet_id: str, title: str, rows: int = 100, cols: int = 26) -> gspread.Worksheet:
        self._throttle()
        ss = self.gc.open_by_key(spreadsheet_id)
        return ss.add_worksheet(title=title, rows=rows, cols=cols)

    def set_header_row(self, spreadsheet_id: str, tab_name: str, headers: List[str]):
        self._throttle()
        ss = self.gc.open_by_key(spreadsheet_id)
        ws = ss.worksheet(tab_name)
        # Write headers in first row
        ws.update(range_name=f"A1:{rowcol_to_a1(1, len(headers))}", values=[headers])
        return ws

    def update_column(self, spreadsheet_id: str, tab_name: str, col_index: int, start_row: int, values: List[str]):
        """Write a single column (vertical) starting at start_row."""
        self._throttle()
        ss = self.gc.open_by_key(spreadsheet_id)
        ws = ss.worksheet(tab_name)
        end_row = start_row + len(values) - 1
        rng = f"{rowcol_to_a1(start_row, col_index)}:{rowcol_to_a1(end_row, col_index)}"
        ws.update(range_name=rng, values=[[v] for v in values])

    def update_cell(self, spreadsheet_id: str, tab_name: str, row: int, col: int, value):
        self._throttle()
        ss = self.gc.open_by_key(spreadsheet_id)
        ws = ss.worksheet(tab_name)
        rng = rowcol_to_a1(row, col)
        ws.update(range_name=rng, values=[[value]])

    def set_checkbox_validation(self, spreadsheet_id: str, tab_name: str, col_index: int, start_row: int, end_row: int):
        """Apply checkbox validation to a column range."""
        self._throttle()
        ss = self.gc.open_by_key(spreadsheet_id)
        ws = ss.worksheet(tab_name)
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
        ss.batch_update(body)
