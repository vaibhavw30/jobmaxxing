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


# ---------------------------------------------------------------------------
# Task 2 — SheetClient protocol + sync_sheet integration tests
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Task 5 — CLI shim + MCP tool delegation
# ---------------------------------------------------------------------------

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
