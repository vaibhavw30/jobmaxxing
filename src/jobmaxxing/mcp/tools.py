import uuid
from datetime import datetime, timedelta, timezone

# The funnel states. Consumed by set_status (a write) to reject typos; query_jobs (a read)
# stays a lenient filter — an unmatched status just returns no rows.
VALID_STATUSES = {
    "new", "routed", "approved_for_tailoring", "tailored", "reviewed", "applied", "rejected",
}
_QUERY_COLS = ["id", "company", "title", "status", "resume_type", "route_confidence", "url", "posted_at"]


def _json_safe(value):
    """uuid/datetime -> str so tool results are JSON-serializable for MCP."""
    if isinstance(value, (uuid.UUID, datetime)):
        return str(value)
    return value


def query_jobs(conn, *, status=None, resume_type=None, company=None,
               since_days=None, limit=50) -> list[dict]:
    """Filtered, capped view of the feed (newest first). limit hard-capped at 200."""
    clauses, params = [], []
    if status is not None:
        clauses.append("status = %s")
        params.append(status)
    if resume_type is not None:
        clauses.append("resume_type = %s")
        params.append(resume_type)
    if company is not None:
        clauses.append("company ilike %s")
        params.append(f"%{company}%")
    if since_days is not None:
        clauses.append("scraped_at >= %s")
        params.append(datetime.now(timezone.utc) - timedelta(days=int(since_days)))
    where = (" where " + " and ".join(clauses)) if clauses else ""
    capped = max(1, min(int(limit), 200))
    rows = conn.execute(
        f"select {', '.join(_QUERY_COLS)} from jobs{where} order by scraped_at desc limit %s",
        (*params, capped),
    ).fetchall()
    return [{c: _json_safe(v) for c, v in zip(_QUERY_COLS, row)} for row in rows]
