"""Google Sheets client behind an injectable interface (real gspread impl + a test fake)."""

import os
from typing import Protocol


class SheetClient(Protocol):
    def header(self) -> list[str]: ...
    def records(self) -> list[dict]: ...           # header-keyed dicts, each with a 1-based "_row"
    def ensure_header(self, header: list[str]) -> None: ...
    def append_rows(self, rows: list[list]) -> None: ...
    def update_cells(self, updates: list[tuple]) -> None: ...   # [(row, col_name, value), ...]


class GspreadClient:
    """Real SheetClient over gspread + a Google service account. Lazily imports gspread so the
    package imports without the 'sheets' extra. Reads GSHEET_ID + GOOGLE_SERVICE_ACCOUNT_FILE."""

    def __init__(self):
        import gspread
        from google.oauth2.service_account import Credentials
        key_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
        sheet_id = os.environ.get("GSHEET_ID")
        if not key_path or not sheet_id:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_FILE and GSHEET_ID must be set (see .env.example)")
        creds = Credentials.from_service_account_file(
            key_path, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        self._gspread = gspread
        self._ws = gspread.authorize(creds).open_by_key(sheet_id).sheet1

    def header(self) -> list[str]:
        return self._ws.row_values(1)

    def records(self) -> list[dict]:
        # row 1 is the header; data starts at row 2
        return [{**r, "_row": i + 2} for i, r in enumerate(self._ws.get_all_records())]

    def ensure_header(self, header: list[str]) -> None:
        if self._ws.row_values(1) != header:
            self._ws.update(values=[header], range_name="A1")

    def append_rows(self, rows: list[list]) -> None:
        self._ws.append_rows(rows, value_input_option="RAW")

    def update_cells(self, updates: list[tuple]) -> None:
        hdr = self.header()
        cells = [self._gspread.Cell(row, hdr.index(col) + 1, value) for (row, col, value) in updates]
        if cells:
            self._ws.update_cells(cells)
