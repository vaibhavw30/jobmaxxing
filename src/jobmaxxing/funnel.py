"""Pure funnel helpers — no DB, no Flask, no external I/O.

This is the single source of truth for:
  * plain_text       — HTML-to-plaintext for spreadsheet cells
  * decision_to_status — operator decision cells -> funnel status
  * VALID_STATUSES   — the complete set of funnel states
  * ROUTED_JOBS_SQL  — canonical SELECT for routed jobs
"""

import html
import re

_MAX_JD_CHARS = 40000     # Sheets cell limit is 50k; leave headroom

VALID_STATUSES = {
    "new", "routed", "approved_for_tailoring", "tailored", "reviewed", "applied", "rejected",
}

TRIAGE_COLUMNS = ("id", "company", "title", "description", "resume_type", "status", "posted_at", "url")

ROUTED_JOBS_SQL = (
    f"select {', '.join(TRIAGE_COLUMNS)} from jobs where resume_type is not null order by scraped_at desc"
)


def plain_text(html_or_text, limit: int = _MAX_JD_CHARS) -> str:
    """Strip HTML tags to plain text (decoding entities so the operator sees real characters,
    not &amp;/&nbsp;) and truncate for a spreadsheet cell."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", html_or_text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def decision_to_status(interested, applied, current: str) -> str | None:
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
