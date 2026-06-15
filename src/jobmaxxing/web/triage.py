"""Triage DB layer — fetch routed jobs + apply operator decisions.

No Flask import. Takes a live psycopg conn as a parameter.
"""

from ..funnel import TRIAGE_COLUMNS, decision_to_status, plain_text

# Columns rendered by the web table: the canonical funnel set plus route_confidence
# (a display/relevance signal not part of the Sheets-facing TRIAGE_COLUMNS).
_DISPLAY_COLS = (*TRIAGE_COLUMNS, "route_confidence")

# DEFAULT_LIMIT == MAX_LIMIT by design: the table renders up to the cap in one page
# (no pagination yet); the "showing N of M" indicator surfaces any truncation.
DEFAULT_LIMIT = 500
MAX_LIMIT = 500


# Jobs with route_confidence below this are demoted (second tier) in the default order.
# 0.4 matches the provisional title-only (route_method='llm_title') confidence cap.
RELEVANCE_FLOOR = 0.4

# Whitelist of clickable-header sort keys -> (sql_expression, default_direction, secondary).
# Expressions are FIXED strings (never user input) -> no SQL injection.
_SORTS = {
    "posted":  ("posted_at",        "desc", ""),
    "company": ("lower(company)",   "asc",  ""),
    "type":    ("resume_type",      "asc",  ", posted_at desc"),
    "conf":    ("route_confidence", "desc", ", posted_at desc"),
}


def _order_by(sort, direction):
    """Build an ORDER BY from the whitelist. Unknown sort -> the 'recent + relevant' default."""
    if sort in _SORTS:
        expr, default_dir, secondary = _SORTS[sort]
        d = direction if direction in ("asc", "desc") else default_dir
        return f"order by {expr} {d}{secondary}, id asc"
    # Default: high-confidence tier first, newest posting first within it.
    # RELEVANCE_FLOOR is a trusted constant, formatted as a literal (not user input).
    return (f"order by (coalesce(route_confidence, 1.0) < {RELEVANCE_FLOOR}) asc,"
            f" posted_at desc nulls last, id asc")


def _build_where(status, statuses, resume_type):
    """Build the shared WHERE clause + params for fetch/count. Routed jobs only."""
    clauses = ["resume_type is not null"]
    params: list = []
    statuses_list = list(statuses) if statuses is not None else None
    if statuses is not None and not statuses_list:
        raise ValueError("statuses must be non-empty when provided")
    if statuses_list:
        placeholders = ", ".join(["%s"] * len(statuses_list))
        clauses.append(f"status in ({placeholders})")
        params.extend(statuses_list)
    elif status is not None:
        clauses.append("status = %s")
        params.append(status)
    if resume_type is not None:
        clauses.append("resume_type = %s")
        params.append(resume_type)
    return " and ".join(clauses), params


def fetch_triage_rows(conn, *, status=None, statuses=None, resume_type=None,
                      sort=None, direction=None, limit=DEFAULT_LIMIT) -> list[dict]:
    """Return routed jobs (resume_type IS NOT NULL) as a list of column-keyed dicts.

    Filters: status= (single), statuses= (IN list; precedence over status=), resume_type=.
    Sorting via _order_by (real impl in Task 2). description returned as plain text.
    Capped at MAX_LIMIT rows.
    """
    where, params = _build_where(status, statuses, resume_type)
    order = _order_by(sort, direction)
    capped = max(1, min(int(limit), MAX_LIMIT))
    sql = f"select {', '.join(_DISPLAY_COLS)} from jobs where {where} {order} limit %s"
    rows = conn.execute(sql, params + [capped]).fetchall()

    result = []
    for row in rows:
        d = dict(zip(_DISPLAY_COLS, row))
        d["description"] = plain_text(d["description"])
        result.append(d)
    return result


def count_triage(conn, *, status=None, statuses=None, resume_type=None) -> int:
    """Total rows matching the same filters as fetch_triage_rows, ignoring sort/limit."""
    where, params = _build_where(status, statuses, resume_type)
    return conn.execute(f"select count(*) from jobs where {where}", params).fetchone()[0]


def apply_decision(conn, job_id, *, interested=None, applied=None) -> dict:
    """Apply an operator decision (interested/applied tokens) to a job's funnel status.

    Returns {"job_id": str, "status": str, "changed": bool}.
    Raises ValueError if the job does not exist.
    String tokens only — do not pass Python booleans.
    """
    row = conn.execute("select status from jobs where id=%s", (job_id,)).fetchone()
    if row is None:
        raise ValueError(f"no job with id {job_id}")
    current = row[0]

    new = decision_to_status(interested or "", applied or "", current)

    if new is None:
        return {"job_id": str(job_id), "status": current, "changed": False}

    with conn.transaction():
        cur = conn.execute("update jobs set status=%s where id=%s", (new, job_id))
        if cur.rowcount == 0:
            raise ValueError(f"no job with id {job_id}")

    return {"job_id": str(job_id), "status": new, "changed": True}


def reset_to_routed(conn, job_id) -> dict:
    """Reset a job to 'routed', guarded to reversible statuses only.

    Statuses in (new, routed, approved_for_tailoring, rejected) are reset.
    tailored / applied are silently skipped (changed: False) — no exception raised.
    """
    with conn.transaction():
        cur = conn.execute(
            "update jobs set status='routed'"
            " where id=%s and status in ('new','routed','approved_for_tailoring','rejected')",
            (job_id,),
        )
    return {"job_id": str(job_id), "status": "routed", "changed": cur.rowcount > 0}
