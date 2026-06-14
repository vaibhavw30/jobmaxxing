import json
import uuid
from datetime import datetime, timedelta, timezone

from ..llm.client import complete as default_complete
from ..routing.config import load_routing_config
from ..routing.route import route_one, set_manual
from ..routing.types import Budget
from ..tailoring.rubric import load_rubric
from ..tailoring.tailor import approve as _tailoring_approve
from ..tailoring.tailor import tailor_job

# The funnel states. Consumed by set_status (a write) to reject typos; query_jobs (a read)
# stays a lenient filter — an unmatched status just returns no rows.
VALID_STATUSES = {
    "new", "routed", "approved_for_tailoring", "tailored", "reviewed", "applied", "rejected",
}
_QUERY_COLS = ["id", "company", "title", "status", "resume_type", "route_confidence", "url", "posted_at"]
_QUEUE_COLS = ["id", "company", "title", "url", "resume_type", "route_confidence", "scraped_at"]


def _json_safe(value):
    """uuid/datetime -> str so tool results are JSON-serializable for MCP."""
    if isinstance(value, (uuid.UUID, datetime)):
        return str(value)
    return value


def nightly_queue(conn, *, limit=50) -> list[dict]:
    """The operator's manual-capture worklist: relevant, still-JD-less jobs both the headless
    worker and find-elsewhere gave up on (from the nightly_queue view). limit hard-capped at 200."""
    capped = max(1, min(int(limit), 200))
    rows = conn.execute(
        f"select {', '.join(_QUEUE_COLS)} from nightly_queue limit %s", (capped,)
    ).fetchall()
    return [{c: _json_safe(v) for c, v in zip(_QUEUE_COLS, row)} for row in rows]


_RECOVER_CAP = 2   # must match recover_jd's cap so find-elsewhere won't re-grab a rejected JD


def reject_recovered(conn, job_id) -> dict:
    """Reject a wrong recovered JD: clear the description, cap recover_attempts so find-elsewhere
    won't re-grab it, keep resume_type so the job drops back into nightly_queue for manual capture.
    Guarded to jd_source='recovered' so it can't accidentally wipe an ATS/manual JD."""
    with conn.transaction():
        cur = conn.execute(
            "update jobs set description=null, jd_source=null, "
            "recover_attempts=greatest(recover_attempts, %s) "
            "where id=%s and jd_source='recovered'",
            (_RECOVER_CAP, job_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"no recovered job with id {job_id}")
    return {"job_id": str(job_id), "status": "rejected_recovered"}


def set_description(conn, job_id, text) -> dict:
    """Ingest a JD the operator obtained (pasted, or fetched by Claude-in-Chrome). Writes the
    description, marks jd_source='manual', and resets resume_type/route_method to NULL (the reset
    contract) so the next route_new re-routes it with the JD — then it can be approved + tailored."""
    text = (text or "").strip()
    if not text:
        raise ValueError("description text is empty")
    with conn.transaction():
        cur = conn.execute(
            "update jobs set description=%s, jd_source='manual', resume_type=null, route_method=null "
            "where id=%s",
            (text, job_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"no job with id {job_id}")
    return {"job_id": str(job_id), "jd_source": "manual", "chars": len(text)}


def query_jobs(conn, *, status=None, resume_type=None, company=None,
               jd_source=None, since_days=None, limit=50) -> list[dict]:
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
    if jd_source is not None:
        clauses.append("jd_source = %s")
        params.append(jd_source)
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


def preview_route(conn, job_id, *, rerun=False, config=None, llm_complete=None) -> dict:
    """The stored route; with rerun=True, also what the router WOULD assign now (not persisted)."""
    row = conn.execute(
        "select title, description, resume_type, route_method, route_confidence from jobs where id=%s",
        (job_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no job with id {job_id}")
    title, description, rtype, method, conf = row
    out = {"stored": {"resume_type": rtype, "route_method": method, "route_confidence": conf}}
    if rerun:
        cfg = config if config is not None else load_routing_config()
        do_llm = llm_complete if llm_complete is not None else default_complete
        decision = route_one(title, description, cfg, llm_complete=do_llm, budget=Budget(remaining=1))
        # `stored` uses DB column names; `rerun` uses RouteDecision field names (method/confidence)
        # — intentional, so the two blocks are visibly distinct in the operator's chat.
        out["rerun"] = {
            "resume_type": decision.resume_type,
            "method": decision.method,
            "confidence": decision.confidence,
        }
    return out


def set_route(conn, job_id, resume_type) -> dict:
    """Manual routing override (route_method='manual'; never auto-re-routed)."""
    set_manual(conn, job_id, resume_type)
    return {"job_id": str(job_id), "resume_type": resume_type, "route_method": "manual"}


def approve(conn, job_id) -> dict:
    """Gate a job for tailoring (status -> approved_for_tailoring)."""
    _tailoring_approve(conn, job_id)
    return {"job_id": str(job_id), "status": "approved_for_tailoring"}


def set_status(conn, job_id, status) -> dict:
    """Move a job through the funnel (incl. applied/rejected — the human gate)."""
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status {status!r}; must be one of {sorted(VALID_STATUSES)}")
    with conn.transaction():
        cur = conn.execute("update jobs set status=%s where id=%s", (status, job_id))
        if cur.rowcount == 0:
            raise ValueError(f"no job with id {job_id}")  # rolls back the (no-op) update
    return {"job_id": str(job_id), "status": status}


def tailor(conn, job_id, *, store, complete, compile_fn) -> dict:
    """Run the Phase-3 tailoring loop for an approved job; return the review summary."""
    return tailor_job(conn, job_id, store=store, complete=complete, compile_fn=compile_fn,
                      rubric_loader=load_rubric)


def get_review(store, job_id) -> dict:
    """Fetch review.json + diff.txt from storage and return both inline."""
    review = json.loads(store.get_artifact(job_id, "review.json").decode("utf-8"))
    diff = store.get_artifact(job_id, "diff.txt").decode("utf-8")
    return {"review": review, "diff": diff}
