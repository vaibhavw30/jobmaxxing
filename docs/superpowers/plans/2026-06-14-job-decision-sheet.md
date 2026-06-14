# Job Decision Sheet Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local two-way Google Sheets sync — push routed jobs' data columns to a sheet, pull the operator's interested/applied marks back into the funnel.

**Architecture:** New local-only `sheets/` package: pure sync logic (`sync.py`) behind an injectable `SheetClient` (real `GspreadClient`, fake for tests), a `python -m jobmaxxing.sync_sheet` CLI, and an MCP `sync_sheet` tool. `gspread`/`google-auth` live in an opt-in `sheets` extra (lazy-imported) so CI stays lean.

**Tech Stack:** Python 3.12, psycopg3, gspread + google-auth (opt-in extra), FastMCP, pytest + pytest-postgresql.

**Spec:** `docs/superpowers/specs/2026-06-14-job-decision-sheet-design.md`

---

## File structure

- Create `src/jobmaxxing/sheets/__init__.py`, `sheets/sync.py` (constants, `_plain`, `_intended_status`, `sync_sheet`, `main`), `sheets/client.py` (`SheetClient` Protocol + `GspreadClient`).
- Create `src/jobmaxxing/sync_sheet.py` (CLI shim).
- Modify `src/jobmaxxing/mcp/tools.py` (`sync_sheet` wrapper) + `src/jobmaxxing/mcp/server.py` (register).
- Modify `pyproject.toml` (`sheets` extra) + `.env.example`.
- Create `tests/test_sheet_sync.py` (unit + integration, fake client) + `tests/test_sheet_sync_e2e.py` (skip-by-default).
- Modify `README.md`.

DB tests run with Postgres on PATH: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"` then `uv run pytest`.

---

### Task 1: `sync.py` pure helpers (`_plain`, `_intended_status`, constants)

**Files:**
- Create: `src/jobmaxxing/sheets/__init__.py`, `src/jobmaxxing/sheets/sync.py`
- Test: `tests/test_sheet_sync.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sheet_sync.py`:

```python
from jobmaxxing.sheets.sync import DATA_COLS, DECISION_COLS, HEADER, _intended_status, _plain


def test_header_is_data_then_decision():
    assert HEADER == DATA_COLS + DECISION_COLS
    assert DATA_COLS[0] == "job_id" and DECISION_COLS == ["interested", "applied"]


def test_plain_strips_html_collapses_and_truncates():
    assert _plain("<p>Hello   <b>world</b></p>") == "Hello world"
    assert _plain(None) == ""
    assert len(_plain("x" * 50000, limit=100)) == 100


def test_intended_status_applied_wins():
    assert _intended_status("Yes", "TRUE", "routed") == "applied"
    assert _intended_status("", "true", "new") == "applied"
    assert _intended_status("", "TRUE", "applied") is None       # already applied -> no-op


def test_intended_status_yes_only_from_new_or_routed():
    assert _intended_status("Yes", "", "routed") == "approved_for_tailoring"
    assert _intended_status("interested", "", "new") == "approved_for_tailoring"
    assert _intended_status("Yes", "", "tailored") is None       # no regress
    assert _intended_status("Yes", "", "reviewed") is None


def test_intended_status_no_rejects_and_blank_noops():
    assert _intended_status("No", "", "routed") == "rejected"
    assert _intended_status("not interested", "", "tailored") == "rejected"
    assert _intended_status("No", "", "rejected") is None        # already rejected
    assert _intended_status("", "", "routed") is None
    assert _intended_status("maybe", "FALSE", "routed") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_sheet_sync.py -v`
Expected: FAIL — `ModuleNotFoundError: jobmaxxing.sheets.sync`.

- [ ] **Step 3: Implement the constants + helpers**

Create `src/jobmaxxing/sheets/__init__.py` (empty). Create `src/jobmaxxing/sheets/sync.py`:

```python
"""Two-way Google Sheets sync for the operator decision sheet. Run LOCALLY:
python -m jobmaxxing.sync_sheet"""

import logging
import re

import psycopg

from ..config import load_settings

logger = logging.getLogger(__name__)

DATA_COLS = ["job_id", "company", "title", "description", "resume_type", "status", "posted_at", "url"]
DECISION_COLS = ["interested", "applied"]
HEADER = DATA_COLS + DECISION_COLS
_MAX_JD_CHARS = 40000     # Sheets cell limit is 50k; leave headroom


def _plain(html_or_text, limit: int = _MAX_JD_CHARS) -> str:
    """Strip HTML tags to plain text and truncate for a spreadsheet cell."""
    text = re.sub(r"<[^>]+>", " ", html_or_text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _intended_status(interested, applied, current: str) -> str | None:
    """Map the operator's decision cells to a funnel status, with a no-regress guard.
    Returns the new status, or None for no change."""
    if str(applied).strip().lower() in ("true", "yes", "1", "✓"):
        return "applied" if current != "applied" else None
    i = str(interested).strip().lower()
    if i in ("no", "n", "not interested", "false") and current != "rejected":
        return "rejected"
    if i in ("yes", "y", "interested", "true") and current in ("new", "routed"):
        return "approved_for_tailoring"
    return None
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_sheet_sync.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/sheets/__init__.py src/jobmaxxing/sheets/sync.py tests/test_sheet_sync.py
git commit -m "feat(sheets): sync constants + _plain + _intended_status (no-regress mapping)"
```

---

### Task 2: `SheetClient` protocol + `sync_sheet` worker

**Files:**
- Create: `src/jobmaxxing/sheets/client.py`
- Modify: `src/jobmaxxing/sheets/sync.py`
- Test: `tests/test_sheet_sync.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sheet_sync.py`:

```python
import psycopg
import pytest

from jobmaxxing.migrate import apply_migrations
from jobmaxxing.sheets.sync import sync_sheet


@pytest.fixture
def conn(postgresql):
    dsn = (f"host={postgresql.info.host} port={postgresql.info.port} "
           f"dbname={postgresql.info.dbname} user={postgresql.info.user}")
    with psycopg.connect(dsn) as c:
        apply_migrations(c)
        yield c


def _insert(conn, *, dedupe_key, resume_type="swe", status="routed", description="<p>jd</p>",
            company="Acme", title="SWE Intern"):
    conn.execute(
        "insert into jobs (dedupe_key, source, company, title, url, description, resume_type, status) "
        "values (%s,'github:simplify',%s,%s,%s,%s,%s,%s)",
        (dedupe_key, company, title, f"https://x/{dedupe_key}", description, resume_type, status),
    )
    conn.commit()
    return str(conn.execute("select id from jobs where dedupe_key=%s", (dedupe_key,)).fetchone()[0])


class FakeSheet:
    """In-memory SheetClient for tests. rows: list of header-keyed dicts incl '_row'."""
    def __init__(self, rows=None):
        self._header = []
        self._rows = rows or []
        self.appended = []
        self.updates = []
    def header(self): return self._header
    def records(self): return self._rows
    def ensure_header(self, header): self._header = list(header)
    def append_rows(self, rows): self.appended.extend(rows)
    def update_cells(self, updates): self.updates.extend(updates)


def test_push_appends_only_routed_jobs_with_stripped_jd(conn):
    jid = _insert(conn, dedupe_key="s|ok")
    _insert(conn, dedupe_key="s|norel", resume_type=None)          # unrouted -> excluded
    fake = FakeSheet()
    counts = sync_sheet(conn, fake)
    assert counts["appended"] == 1
    row = fake.appended[0]
    assert row[0] == jid and row[3] == "jd"                        # job_id, stripped description
    assert row[-2:] == ["", ""]                                   # decision cells blank
    assert fake._header == ["job_id", "company", "title", "description", "resume_type",
                            "status", "posted_at", "url", "interested", "applied"]


def test_pull_maps_decisions_with_no_regress(conn):
    a = _insert(conn, dedupe_key="p|yes", status="routed")
    b = _insert(conn, dedupe_key="p|no", status="routed")
    c = _insert(conn, dedupe_key="p|app", status="routed")
    d = _insert(conn, dedupe_key="p|tail", status="tailored")     # interested=Yes must NOT regress
    fake = FakeSheet(rows=[
        {"job_id": a, "interested": "Yes", "applied": "", "_row": 2},
        {"job_id": b, "interested": "No", "applied": "", "_row": 3},
        {"job_id": c, "interested": "", "applied": "TRUE", "_row": 4},
        {"job_id": d, "interested": "Yes", "applied": "", "_row": 5},
    ])
    sync_sheet(conn, fake)
    s = dict(conn.execute("select id, status from jobs").fetchall())
    import uuid as _uuid
    assert s[_uuid.UUID(a)] == "approved_for_tailoring"
    assert s[_uuid.UUID(b)] == "rejected"
    assert s[_uuid.UUID(c)] == "applied"
    assert s[_uuid.UUID(d)] == "tailored"                         # not regressed


def test_push_updates_changed_data_cell_not_decisions(conn):
    jid = _insert(conn, dedupe_key="u|1", status="approved_for_tailoring")
    # sheet already has the row but with a stale status; decision cells set by the operator
    fake = FakeSheet(rows=[{"job_id": jid, "company": "Acme", "title": "SWE Intern",
                            "description": "jd", "resume_type": "swe", "status": "routed",
                            "posted_at": "", "url": f"https://x/u|1",
                            "interested": "Yes", "applied": "", "_row": 2}])
    sync_sheet(conn, fake)
    assert fake.appended == []                                    # no re-append
    cols_updated = {u[1] for u in fake.updates}
    assert "status" in cols_updated                              # stale status cell refreshed
    assert "interested" not in cols_updated and "applied" not in cols_updated   # decisions untouched


def test_sync_is_idempotent(conn):
    _insert(conn, dedupe_key="i|1")
    fake = FakeSheet()
    sync_sheet(conn, fake)                                        # first run: appends 1
    # reflect the append back into the fake's records, then re-run
    appended = fake.appended[0]
    fake._rows = [dict(zip(fake._header, appended), _row=2)]
    fake.appended.clear(); fake.updates.clear()
    counts = sync_sheet(conn, fake)
    assert counts["appended"] == 0 and fake.updates == []        # nothing changed
```

- [ ] **Step 2: Run to verify failure**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_sheet_sync.py -k "push or pull or idempotent" -v`
Expected: FAIL — `ImportError: cannot import name 'sync_sheet'`.

- [ ] **Step 3: Implement the protocol + worker**

Create `src/jobmaxxing/sheets/client.py`:

```python
"""Google Sheets client behind an injectable interface (real gspread impl + a test fake)."""

import os
from typing import Protocol


class SheetClient(Protocol):
    def header(self) -> list[str]: ...
    def records(self) -> list[dict]: ...           # header-keyed dicts, each with a 1-based "_row"
    def ensure_header(self, header: list[str]) -> None: ...
    def append_rows(self, rows: list[list]) -> None: ...
    def update_cells(self, updates: list[tuple]) -> None: ...   # [(row, col_name, value), ...]
```

Append to `src/jobmaxxing/sheets/sync.py` (add `from .client import SheetClient` to the imports):

```python
def sync_sheet(conn, client: SheetClient) -> dict:
    """Pull the operator's decisions into the funnel, then push routed jobs' data to the sheet.
    Returns {appended, updated, pulled_approved_for_tailoring, pulled_rejected, pulled_applied}."""
    client.ensure_header(HEADER)
    sheet_rows = {str(r.get("job_id")): r for r in client.records() if r.get("job_id")}

    # 1) PULL: sheet decisions -> DB status (no-regress)
    pulled = {"approved_for_tailoring": 0, "rejected": 0, "applied": 0}
    db = {str(jid): status for jid, status in
          conn.execute("select id, status from jobs where resume_type is not null").fetchall()}
    status_updates = []
    for jid, row in sheet_rows.items():
        cur = db.get(jid)
        if cur is None:
            continue
        new_status = _intended_status(row.get("interested"), row.get("applied"), cur)
        if new_status:
            status_updates.append((new_status, jid))
            pulled[new_status] += 1
    if status_updates:
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany("update jobs set status=%s where id=%s", status_updates)

    # 2) PUSH: routed DB jobs -> data columns (append new, refresh existing; never touch decisions)
    rows = conn.execute(
        "select id, company, title, description, resume_type, status, posted_at, url "
        "from jobs where resume_type is not null order by scraped_at desc").fetchall()
    new_rows, cell_updates, updated = [], [], 0
    for (jid, company, title, desc, rtype, status, posted_at, url) in rows:
        data = [str(jid), company, title, _plain(desc), rtype, status,
                str(posted_at) if posted_at else "", url]
        existing = sheet_rows.get(str(jid))
        if existing is None:
            new_rows.append(data + ["", ""])          # blank decision cells
        else:
            for col, val in zip(DATA_COLS, data):
                if str(existing.get(col, "")) != str(val):
                    cell_updates.append((existing["_row"], col, val))
            updated += 1
    if new_rows:
        client.append_rows(new_rows)
    if cell_updates:
        client.update_cells(cell_updates)

    counts = {"appended": len(new_rows), "updated": updated,
              **{f"pulled_{k}": v for k, v in pulled.items()}}
    logger.info("sheet sync: %s", counts)
    return counts
```

- [ ] **Step 4: Run to verify pass**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_sheet_sync.py -v`
Expected: PASS (all unit + 4 integration tests).

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/sheets/client.py src/jobmaxxing/sheets/sync.py tests/test_sheet_sync.py
git commit -m "feat(sheets): SheetClient protocol + sync_sheet (pull decisions, push data)"
```

---

### Task 3: `sheets` optional dependency + `.env.example`

**Files:**
- Modify: `pyproject.toml`, `uv.lock`, `.env.example`

- [ ] **Step 1: Add the optional-dependency group + env vars**

In `pyproject.toml`, under the existing `[project.optional-dependencies]`, add the `sheets` extra (next to `headless`):

```toml
[project.optional-dependencies]
headless = ["playwright>=1.40"]
sheets = ["gspread>=6", "google-auth>=2"]
```

In `.env.example`, add:

```
# Google Sheets decision sheet (local sync_sheet worker; uv sync --extra sheets)
GSHEET_ID=
GOOGLE_SERVICE_ACCOUNT_FILE=
```

- [ ] **Step 2: Lock + verify the default env stays lean**

Run: `uv lock && uv sync --frozen --no-dev && uv run python -c "import importlib.util; assert importlib.util.find_spec('gspread') is None; print('gspread absent from default env: OK')"`
Expected: prints OK (CI/default env never installs gspread).

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock .env.example
git commit -m "build(sheets): add gspread/google-auth as the opt-in 'sheets' extra + env vars"
```

---

### Task 4: `GspreadClient` (the real Sheets client)

**Files:**
- Modify: `src/jobmaxxing/sheets/client.py`
- Test: `tests/test_sheet_sync_e2e.py`

> No unit test for `GspreadClient` — it's the external-API boundary (like `PlaywrightFetcher`), exercised by the skip-by-default e2e test and the operator's first run. It lazily imports `gspread` so the module imports fine without the `sheets` extra.

- [ ] **Step 1: Write the skip-by-default e2e test**

Create `tests/test_sheet_sync_e2e.py`:

```python
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
```

- [ ] **Step 2: Verify it is collected-but-skipped in a normal run**

Run: `uv run pytest tests/test_sheet_sync_e2e.py -v`
Expected: `1 skipped`.

- [ ] **Step 3: Implement `GspreadClient`**

Append to `src/jobmaxxing/sheets/client.py`:

```python
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
            self._ws.update([header], "A1")

    def append_rows(self, rows: list[list]) -> None:
        self._ws.append_rows(rows, value_input_option="RAW")

    def update_cells(self, updates: list[tuple]) -> None:
        hdr = self.header()
        cells = [self._gspread.Cell(row, hdr.index(col) + 1, value) for (row, col, value) in updates]
        if cells:
            self._ws.update_cells(cells)
```

> **gspread 6.x note:** `GspreadClient` is the external-API boundary (no unit test). The method calls above target gspread 6.x — but confirm the exact signatures against the installed version when running the e2e: `worksheet.update(values, range_name)` arg order changed across gspread majors (use a keyword if unsure: `.update(values=[header], range_name="A1")`), `get_all_records()` returns header-keyed dicts, `append_rows(values, value_input_option=...)`, and `Cell(row, col, value)` + `update_cells([...])` are stable. If the e2e (`JOBMAXXING_E2E=1 uv run --extra sheets pytest tests/test_sheet_sync_e2e.py`) errors on an arg-order mismatch, adjust the call — the `SheetClient` interface and all sync logic are unaffected.

- [ ] **Step 4: Verify it parses + the e2e still skips**

Run: `uv run python -c "import ast; ast.parse(open('src/jobmaxxing/sheets/client.py').read()); print('parse OK')" && uv run pytest tests/test_sheet_sync_e2e.py -q`
Expected: `parse OK` then `1 skipped`.

- [ ] **Step 5: Commit**

```bash
git add src/jobmaxxing/sheets/client.py tests/test_sheet_sync_e2e.py
git commit -m "feat(sheets): GspreadClient (service-account gspread impl) + skip-by-default e2e"
```

---

### Task 5: CLI + MCP tool + README

**Files:**
- Modify: `src/jobmaxxing/sheets/sync.py` (add `main`)
- Create: `src/jobmaxxing/sync_sheet.py`
- Modify: `src/jobmaxxing/mcp/tools.py`, `src/jobmaxxing/mcp/server.py`, `README.md`
- Test: `tests/test_sheet_sync.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sheet_sync.py`:

```python
def test_cli_shim_exposes_main():
    import jobmaxxing.sync_sheet as cli
    from jobmaxxing.sheets.sync import main
    assert cli.main is main


def test_mcp_sync_sheet_tool_delegates(conn):
    from jobmaxxing.mcp.tools import sync_sheet as tool_sync
    _insert(conn, dedupe_key="m|1")
    fake = FakeSheet()
    counts = tool_sync(conn, client=fake)
    assert counts["appended"] == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_sheet_sync.py -k "cli_shim or mcp_sync_sheet" -v`
Expected: FAIL — `ModuleNotFoundError: jobmaxxing.sync_sheet` / `cannot import name 'sync_sheet'` from tools.

- [ ] **Step 3: Implement `main`, the CLI shim, and the MCP tool**

Append to `src/jobmaxxing/sheets/sync.py`:

```python
def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .client import GspreadClient
    settings = load_settings()
    with psycopg.connect(settings.database_url) as conn:
        print(f"sheet sync: {sync_sheet(conn, GspreadClient())}")
```

Create `src/jobmaxxing/sync_sheet.py`:

```python
"""CLI shim: `python -m jobmaxxing.sync_sheet` (run LOCALLY; needs the `sheets` extra)."""

from .sheets.sync import main

if __name__ == "__main__":
    main()
```

In `src/jobmaxxing/mcp/tools.py`, add:

```python
def sync_sheet(conn, *, client) -> dict:
    """Run the two-way Google Sheets sync (pull decisions -> funnel, push routed jobs -> sheet)."""
    from ..sheets.sync import sync_sheet as _sync
    return _sync(conn, client)
```

In `src/jobmaxxing/mcp/server.py`, register it (builds the real client):

```python
@mcp.tool()
def sync_sheet() -> dict:
    """Sync the Google decision sheet both ways: apply your interested/applied marks to the funnel,
    then refresh the sheet with the latest routed jobs. Returns the counts."""
    from ..sheets.client import GspreadClient
    with _conn() as conn:
        return tools.sync_sheet(conn, client=GspreadClient())
```

- [ ] **Step 4: Run to verify pass + full suite**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_sheet_sync.py -v && uv run python -c "import jobmaxxing.mcp.server; print('server import ok')"`
Expected: PASS; `server import ok`.

- [ ] **Step 5: Document in the README**

Add to `README.md`, near the MCP/operator sections:

```markdown
### Job decision sheet (Google Sheets, two-way)

Triage routed jobs in a spreadsheet instead of one MCP call at a time. One-time setup:

1. Create a Google Cloud service account, enable the Google Sheets API, download its JSON key.
2. Create a Google Sheet, share it (Editor) with the service account's email.
3. Set `GSHEET_ID` and `GOOGLE_SERVICE_ACCOUNT_FILE` in `.env`; `uv sync --extra sheets`.

Then sync (locally, or the `sync_sheet` MCP tool): `uv run --extra sheets python -m jobmaxxing.sync_sheet`.
It pushes routed jobs (company, title, JD, status, …) into the sheet and pulls your decision columns
back into the funnel: **interested = Yes** → queued for tailoring, **No** → rejected, **applied** → applied.
Mark jobs in the sheet, re-run the sync, then your local `tailor` run picks up the interested ones.
```

- [ ] **Step 6: Run the full suite**

Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest -q`
Expected: all pass; the e2e tests (Sheets/pdflatex/Workday/claude-cli) skip by design.

- [ ] **Step 7: Commit**

```bash
git add src/jobmaxxing/sheets/sync.py src/jobmaxxing/sync_sheet.py src/jobmaxxing/mcp/tools.py src/jobmaxxing/mcp/server.py README.md tests/test_sheet_sync.py
git commit -m "feat(sheets): sync_sheet CLI + MCP tool + README setup"
```

---

## Done criteria

- `uv run pytest -q` green: unit (`_plain`, `_intended_status` incl. no-regress), integration (push appends only routed + stripped JD, pull drives the funnel without regression, updates data cells not decisions, idempotent), CLI + MCP-tool delegation; e2e skipped by default; all pre-existing tests green.
- The default `uv sync --frozen --no-dev` env has no `gspread` (CI stays lean); `gspread` only via `--extra sheets`.
- Operator can: `uv run --extra sheets python -m jobmaxxing.sync_sheet` (or the `sync_sheet` MCP tool) to push routed jobs to a Google Sheet and pull interested/applied marks into the funnel.
- No CI workflow change; no new *runtime* dependency.
