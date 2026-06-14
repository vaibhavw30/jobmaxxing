"""Two-way Google Sheets sync for the operator decision sheet. Run LOCALLY:
python -m jobmaxxing.sync_sheet"""

import logging
import re

import psycopg

from ..config import load_settings
from .client import SheetClient

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
