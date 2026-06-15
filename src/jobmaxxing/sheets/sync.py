"""Two-way Google Sheets sync for the operator decision sheet. Run LOCALLY:
python -m jobmaxxing.sync_sheet"""

import logging

import psycopg

from ..config import load_settings
from ..funnel import ROUTED_JOBS_SQL, decision_to_status, plain_text
from .client import SheetClient

logger = logging.getLogger(__name__)

DATA_COLS = ["job_id", "company", "title", "description", "resume_type", "status", "posted_at", "url"]
DECISION_COLS = ["interested", "applied"]
HEADER = DATA_COLS + DECISION_COLS

# Back-compat aliases so existing callers / tests that import these names keep working.
_plain = plain_text
_intended_status = decision_to_status


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
    rows = conn.execute(ROUTED_JOBS_SQL).fetchall()
    new_rows, cell_updates, updated = [], [], 0
    for (jid, company, title, desc, rtype, status, posted_at, url) in rows:
        data = [str(jid), company, title, _plain(desc), rtype, status,
                str(posted_at) if posted_at else "", url]
        existing = sheet_rows.get(str(jid))
        if existing is None:
            new_rows.append(data + ["", ""])          # blank decision cells
        else:
            changed = False
            for col, val in zip(DATA_COLS, data):
                if str(existing.get(col, "")) != str(val):
                    cell_updates.append((existing["_row"], col, val))
                    changed = True
            if changed:
                updated += 1                          # rows actually changed (0 on a no-op re-sync)
    if new_rows:
        client.append_rows(new_rows)
    if cell_updates:
        client.update_cells(cell_updates)

    counts = {"appended": len(new_rows), "updated": updated,
              **{f"pulled_{k}": v for k, v in pulled.items()}}
    logger.info("sheet sync: %s", counts)
    return counts


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .client import GspreadClient
    settings = load_settings()
    with psycopg.connect(settings.database_url) as conn:
        print(f"sheet sync: {sync_sheet(conn, GspreadClient())}")
