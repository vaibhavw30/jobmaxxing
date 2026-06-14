"""Real Google Sheets round-trip. Skipped unless JOBMAXXING_E2E=1 AND GSHEET_ID/key are set
(mirrors the other e2e skips). Run: JOBMAXXING_E2E=1 uv run --extra sheets pytest tests/test_sheet_sync_e2e.py -v"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("JOBMAXXING_E2E") != "1" or not os.environ.get("GSHEET_ID"),
    reason="set JOBMAXXING_E2E=1 + GSHEET_ID/GOOGLE_SERVICE_ACCOUNT_FILE to run the live Sheets e2e",
)


def test_live_gspread_header_and_append():
    from jobmaxxing.sheets.client import GspreadClient
    from jobmaxxing.sheets.sync import HEADER
    client = GspreadClient()
    client.ensure_header(HEADER)
    assert client.header() == HEADER
    client.append_rows([["e2e-test-id"] + [""] * (len(HEADER) - 1)])
    assert any(r.get("job_id") == "e2e-test-id" for r in client.records())
