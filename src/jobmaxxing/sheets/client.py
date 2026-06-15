"""Google Sheets client behind an injectable interface (real gspread impl + a test fake)."""

import os
from typing import Protocol


class SheetClient(Protocol):
    def header(self) -> list[str]: ...
    def records(self) -> list[dict]: ...           # header-keyed dicts, each with a 1-based "_row"
    def ensure_header(self, header: list[str]) -> None: ...
    def append_rows(self, rows: list[list]) -> None: ...
    def update_cells(self, updates: list[tuple]) -> None: ...   # [(row, col_name, value), ...]


_SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
                  "https://www.googleapis.com/auth/drive"]


class GspreadClient:
    """Real SheetClient over gspread. Lazily imports gspread so the package imports without the
    'sheets' extra. Auth (GSHEET_ID required either way):
      - GOOGLE_SERVICE_ACCOUNT_FILE set -> service-account key (share the sheet with its email); else
      - Application Default Credentials -> authenticates as YOU; no key, no sharing. Run once:
        `gcloud auth application-default login --scopes=<spreadsheets>,<drive>`.
    ADC is the default because Google's 'Secure by Default' org policy often blocks service-account
    key creation on new projects."""

    def __init__(self):
        import gspread
        sheet_id = os.environ.get("GSHEET_ID")
        if not sheet_id:
            raise RuntimeError("GSHEET_ID must be set (see .env.example)")
        key_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
        if key_path:
            gc = gspread.service_account(filename=key_path)
        else:
            import google.auth                                    # Application Default Credentials (you)
            creds, _ = google.auth.default(scopes=_SHEETS_SCOPES)
            gc = gspread.authorize(creds)
        self._gspread = gspread
        self._ws = gc.open_by_key(sheet_id).sheet1

    def header(self) -> list[str]:
        return self._ws.row_values(1)

    def records(self) -> list[dict]:
        # row 1 is the header; data starts at row 2
        return [{**r, "_row": i + 2} for i, r in enumerate(self._ws.get_all_records())]

    def ensure_header(self, header: list[str]) -> None:
        # The tool owns columns A..(len(header)); compare only those so an operator's OWN extra
        # columns appended AFTER them are never overwritten. (Don't insert columns between ours.)
        if self._ws.row_values(1)[: len(header)] != header:
            self._ws.update(values=[header], range_name="A1")

    def append_rows(self, rows: list[list]) -> None:
        self._ws.append_rows(rows, value_input_option="RAW")

    def update_cells(self, updates: list[tuple]) -> None:
        hdr = self.header()
        cells = [self._gspread.Cell(row, hdr.index(col) + 1, value) for (row, col, value) in updates]
        if cells:
            self._ws.update_cells(cells)
