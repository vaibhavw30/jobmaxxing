"""Triage DB layer — fetch routed jobs + apply operator decisions.

No Flask import. Takes a live psycopg conn as a parameter.
"""

from ..funnel import decision_to_status, plain_text

# Same columns as ROUTED_JOBS_SQL — kept here as the local source of truth so
# we can build parameterized WHERE clauses without appending after ORDER BY.
_TRIAGE_COLS = ["id", "company", "title", "description", "resume_type", "status", "posted_at", "url"]

_MAX_LIMIT = 200


def fetch_triage_rows(conn, *, status=None, resume_type=None, limit=200) -> list[dict]:
    """Return routed jobs (resume_type IS NOT NULL) as a list of column-keyed dicts.

    Optional filters: status=, resume_type=.  Hard-capped at 200 rows.
    description is returned as plain text (HTML stripped).
    """
    clauses = ["resume_type is not null"]
    params: list = []

    if status is not None:
        clauses.append("status = %s")
        params.append(status)
    if resume_type is not None:
        clauses.append("resume_type = %s")
        params.append(resume_type)

    where = " and ".join(clauses)
    capped = max(1, min(int(limit), _MAX_LIMIT))
    params.append(capped)

    sql = (
        f"select {', '.join(_TRIAGE_COLS)} from jobs"
        f" where {where}"
        f" order by scraped_at desc limit %s"
    )
    rows = conn.execute(sql, params).fetchall()

    result = []
    for row in rows:
        d = dict(zip(_TRIAGE_COLS, row))
        d["description"] = plain_text(d["description"])
        result.append(d)
    return result


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
    return {"job_id": str(job_id), "changed": cur.rowcount > 0}
