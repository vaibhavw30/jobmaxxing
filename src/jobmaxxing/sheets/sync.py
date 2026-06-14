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
