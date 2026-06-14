# Spec — Job decision sheet (two-way Google Sheets sync)

**Type:** New feature — an operator decision surface synced two-way with the pipeline.
**Author:** Vaibhav
**Date:** 2026-06-14
**Status:** Approved for planning
**Builds on:** the funnel/status machine (`new → routed → approved_for_tailoring → tailored → reviewed → applied/rejected`) and the Phase-4 MCP. Runs locally (operator-side).

---

## 1. Problem & rationale

The operator needs to triage routed candidates and decide which to pursue. Doing this via MCP tool calls (`approve`/`set_status` one job at a time) is slow at ~3.4k routed jobs. A **Google Sheet** is the natural surface: the pipeline writes the job data, the operator marks decisions in a familiar spreadsheet UI, and a sync feeds those decisions back into the funnel. Google Sheets (vs Excel) was chosen for frictionless **service-account** auth (no Azure app / admin consent / token refresh) and cloud-native concurrent editing (no file locks).

### Scope
**In:** a local-only `sheets/` package (a service-account `SheetClient` behind an injectable interface + the bidirectional `sync` logic), a `python -m jobmaxxing.sync_sheet` CLI, an MCP `sync_sheet` tool, a `sheets` optional-dependency extra, config (`GSHEET_ID` + service-account key path), and tests. Targets routed jobs (`resume_type` set).
**Out:** Excel/OneDrive; auto-tailoring (marking interested only *queues* tailoring — the local `tailor` run still does it); any change to the routing/enrichment pipeline; an applicants column (no data source).

## 2. Source-of-truth model

Two-way, keyed by a hidden `job_id` (the DB uuid) so sheet rows map to DB rows — **no conflicts because each side owns distinct columns**:

- **Pipeline owns the data columns** (pushed DB → sheet): `job_id`, company, title, description, resume_type, status, posted_at, url. The push **never writes the decision columns.**
- **Operator owns the decision columns** (pulled sheet → DB): `interested` (dropdown: Yes / No / blank), `applied` (checkbox). The pull **only reads** these.

Header (fixed column order):
```
job_id | company | title | description | resume_type | status | posted_at | url | interested | applied
\_______________________ pipeline (push) ____________________________/   \____ operator (pull) ____/
```

Each `sync` run does **pull first, then push** — so a just-marked decision is applied to the DB *and then* reflected back in the sheet's `status` column on the same run.

## 3. Funnel mapping (pull), with no-regress guard

For each sheet row (matched to a DB job by `job_id`), compute the intended status from the decision columns and apply it **only when it's a safe forward move** (never regress a job that's already further along):

| Decision cell | → DB status | Guard |
| --- | --- | --- |
| `applied` is truthy (`TRUE`/`Yes`/`1`) | `applied` | always (terminal mark) |
| `interested = No` (`no`/`not interested`/`false`) | `rejected` | from any non-terminal status (explicit rejection) |
| `interested = Yes` (`yes`/`interested`/`true`) | `approved_for_tailoring` | **only if current status ∈ {new, routed}** — don't pull a tailored/reviewed job back |
| `interested` blank | no change | — |

`applied` takes precedence over `interested`. A status change is written only when it differs from the current DB status (idempotent). Decision parsing is lenient (case/whitespace-insensitive; unknown values → no-op).

## 4. Architecture (with code shapes)

New local-only package `src/jobmaxxing/sheets/`:

### 4.1 `client.py` — the injectable Sheets client

```python
from typing import Protocol

class SheetClient(Protocol):
    """The sync logic talks to this; the real impl wraps gspread, the test impl is in-memory."""
    def header(self) -> list[str]: ...
    def records(self) -> list[dict]: ...          # one dict per data row, header-keyed, plus "_row" (1-based row index)
    def ensure_header(self, header: list[str]) -> None: ...   # write header row if missing/changed
    def append_rows(self, rows: list[list]) -> None: ...       # batch-append new rows (one API call)
    def update_cells(self, updates: list[tuple]) -> None: ...  # [(row, col_name, value), ...] — one batch_update
```

`GspreadClient(SheetClient)` lazily imports `gspread`/`google.oauth2.service_account` in `__init__`, authenticates with the service-account key, opens the sheet by `GSHEET_ID`, and implements the protocol against a worksheet. Lazy import keeps CI (no `sheets` extra) clean.

### 4.2 `sync.py` — the bidirectional sync (pure, testable with a fake client)

```python
DATA_COLS = ["job_id", "company", "title", "description", "resume_type", "status", "posted_at", "url"]
DECISION_COLS = ["interested", "applied"]
HEADER = DATA_COLS + DECISION_COLS
_MAX_JD_CHARS = 40000     # Sheets cell limit is 50k; leave headroom


def _plain(html_or_text: str | None, limit: int = _MAX_JD_CHARS) -> str:
    """Strip HTML tags to plain text and truncate for a spreadsheet cell."""
    text = re.sub(r"<[^>]+>", " ", html_or_text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _intended_status(interested, applied, current: str) -> str | None:
    a = str(applied).strip().lower()
    if a in ("true", "yes", "1", "✓"):
        return "applied" if current != "applied" else None
    i = str(interested).strip().lower()
    if i in ("no", "n", "not interested", "false") and current != "rejected":
        return "rejected"
    if i in ("yes", "y", "interested", "true") and current in ("new", "routed"):
        return "approved_for_tailoring"
    return None


def sync_sheet(conn, client: SheetClient) -> dict:
    client.ensure_header(HEADER)
    sheet_rows = {r.get("job_id"): r for r in client.records() if r.get("job_id")}

    # 1) PULL: sheet decisions -> DB status (no-regress)
    pulled = {"approved_for_tailoring": 0, "rejected": 0, "applied": 0}
    db = {str(jid): status for jid, status in conn.execute(
        "select id, status from jobs where resume_type is not null").fetchall()}
    status_updates = []
    for jid, row in sheet_rows.items():
        cur = db.get(str(jid))
        if cur is None:
            continue
        new_status = _intended_status(row.get("interested"), row.get("applied"), cur)
        if new_status:
            status_updates.append((new_status, jid)); pulled[new_status] += 1
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
            new_rows.append(data + ["", ""])    # blank decision cells
        else:
            for col, val in zip(DATA_COLS, data):
                if str(existing.get(col, "")) != str(val):
                    cell_updates.append((existing["_row"], col, val))
            updated += 1
    if new_rows:
        client.append_rows(new_rows)            # one batched API call
    if cell_updates:
        client.update_cells(cell_updates)       # one batched API call

    return {"appended": len(new_rows), "updated": updated,
            **{f"pulled_{k}": v for k, v in pulled.items()}}
```

### 4.3 CLI + MCP

- `src/jobmaxxing/sync_sheet.py` — `main()` (load settings, open `GspreadClient`, run `sync_sheet`, print counts) + the `python -m jobmaxxing.sync_sheet` shim, mirroring `enrich_workday.py`.
- `mcp/tools.py`: `sync_sheet(conn, *, client)` thin wrapper; `mcp/server.py`: a `sync_sheet()` `@mcp.tool()` that builds a `GspreadClient` and runs it (so the operator can trigger a sync conversationally). Returns the counts.

## 5. Config & dependency

- `.env` (+ `.env.example`): `GSHEET_ID` (the sheet's id) and `GOOGLE_SERVICE_ACCOUNT_FILE` (path to the downloaded service-account JSON key). `load_settings` already loads `.env`; the client reads these from the environment.
- `pyproject.toml`: `[project.optional-dependencies] sheets = ["gspread>=6", "google-auth>=2"]`. `gspread`/`google-auth` are imported **lazily** inside `GspreadClient` so the default (CI) env never needs them. Operator setup: `uv sync --extra sheets`.
- Operator one-time: create a Google Cloud service account, enable the Sheets API, download the JSON key, create the sheet, **share it with the service account's email** (Editor), set the two env vars. (Documented in the README.)

## 6. Cadence

A cron entry runs `python -m jobmaxxing.sync_sheet` (e.g. shortly after the nightly local workers) for an unattended bidirectional sync, and the MCP `sync_sheet` tool triggers it on demand. The sync is idempotent — running it repeatedly converges.

## 7. Invariants & error handling

| Invariant | How |
| --- | --- |
| Decision columns never clobbered | The push writes only `DATA_COLS`; appends leave decision cells blank; updates compare/write only data columns. |
| No funnel regression | `_intended_status` only sets `approved_for_tailoring` from `{new, routed}`; `applied`/`rejected` are explicit terminal/operator marks. |
| Idempotent | Status written only when changed; data cells written only when changed; re-running converges. |
| Only relevant jobs in the sheet | Both push and pull are scoped to `resume_type is not null`. |
| CI stays lean | `gspread`/`google-auth` are an opt-in extra, lazy-imported; no CI/workflow change. |
| Sync logic testable without the API | All Sheets I/O behind `SheetClient`; the sync is unit/integration-tested with a fake in-memory client. |
| Operator stays the gate | Marking interested only *queues* tailoring (`approved_for_tailoring`); the local `tailor` run still produces and the operator still reviews. |
| Bad/auth failures fail loudly | `GspreadClient` raises on missing `GSHEET_ID`/key or auth errors (the CLI surfaces it); a malformed decision cell is a safe no-op, not a crash. |

## 8. Testing — pyramid

- **Unit (fake `SheetClient`, no API/DB):**
  - `_plain`: strips tags, collapses whitespace, truncates at the limit; `None`/empty → "".
  - `_intended_status`: `applied` truthy → `applied` (and no-op if already applied); `interested=No` → `rejected` (from non-rejected); `interested=Yes` → `approved_for_tailoring` only from `{new, routed}` (no-op from `tailored`/`reviewed`/`applied`); blank/garbage → `None`.
- **Integration (pytest-postgresql + fake in-memory `SheetClient`):**
  - **Push:** seeds routed + unrouted (`resume_type` null) + has/no description jobs → only routed rows are appended; the unrouted one is excluded; JD is HTML-stripped; a second run updates a changed data cell (e.g. status) without re-appending and **without touching the decision cells** the fake holds.
  - **Pull:** the fake client holds rows with `interested=Yes/No`, `applied=TRUE`, blank → DB status becomes `approved_for_tailoring`/`rejected`/`applied`/unchanged; a `tailored` job with `interested=Yes` is **not** regressed; idempotent on re-run.
  - **Round-trip:** push, set a decision in the fake, pull → DB status updated → next push reflects the new status in the row.
  - Counts dict `{appended, updated, pulled_*}` correct.
- **E2E (skip-by-default, `JOBMAXXING_E2E=1` + `GSHEET_ID`/key set):** a real `GspreadClient` against a scratch sheet — header ensured, a row appended, read back. Operator-run.

## 9. Deliverables

- `src/jobmaxxing/sheets/{__init__,client,sync}.py`; `src/jobmaxxing/sync_sheet.py` CLI.
- `mcp/tools.py` `sync_sheet` + `mcp/server.py` registration.
- `pyproject.toml` `sheets` extra (+ `uv.lock`); `.env.example` entries.
- Tests: `tests/test_sheet_sync.py` (unit + integration with the fake client), `tests/test_sheet_sync_e2e.py` (skip-by-default).
- README: the Google Sheets setup (service account, sharing, env vars) + the operator workflow (mark interested/applied → sync → funnel).
- No CI workflow change; no new *runtime* dependency (the extra is opt-in).

## 10. Open items & risks (named, accepted)

- **Google API quota:** the sync makes O(1) API calls per run — one `get_all_records` (read), one batched `append_rows` (all new rows), one batched `update_cells` (all changed data cells) — NOT one call per row, so the first-sync population of ~3.4k rows is a single append, well within limits.
- **JD in a cell:** stripped to plain text + truncated to 40k chars; the full JD remains in the DB and the `url` column links out.
- **Header drift:** `ensure_header` writes the canonical header; if the operator reorders columns, the sync re-aligns by header name via `records()` (header-keyed), not by position — except `append_row`/`update_cells` assume the canonical column order, so the operator should not reorder the pipeline columns (documented). Decision columns are matched by name.
- **Deletion:** jobs removed from the routed set (rare) are not deleted from the sheet — they linger with a stale status. Acceptable; a future cleanup pass could prune. Out of scope.
