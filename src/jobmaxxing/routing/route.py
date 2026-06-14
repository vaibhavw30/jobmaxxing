import logging
import sys

import psycopg

from ..config import load_settings
from ..llm.client import complete as llm_complete_default
from .config import load_routing_config
from .rules import route_by_rules
from .tiebreaker import resolve
from .types import VALID_TYPES, Budget, RouteDecision

logger = logging.getLogger(__name__)

_DEFER = RouteDecision(resume_type=None, method=None, confidence=0.0)
# A single weakly-scoring candidate: route it deterministically (the LLM has nothing to
# disambiguate), with modest confidence.
_SINGLE_CANDIDATE_CONFIDENCE = 0.5

_INTERNSHIP_MARKERS = (
    "intern", "co-op", "coop", "co op", "student", "apprentic", "new grad",
    "early career", "university", "campus", "trainee",
)


def _looks_like_internship(title: str | None) -> bool:
    """Coarse gate: does the title look like an internship/early-career role? Used to skip
    open-classification (and its LLM call) on obvious non-targets."""
    t = (title or "").lower()
    return any(m in t for m in _INTERNSHIP_MARKERS)


def route_one(
    title: str | None, description: str | None, config: dict, *, llm_complete, budget: Budget
) -> RouteDecision:
    """Route a single posting. Title-first deterministic; the LLM is used only for
    ambiguous JD-bearing rows with >1 candidate, within budget; otherwise defer."""
    outcome = route_by_rules(title, description, config)
    if outcome.decision == "routed":
        return RouteDecision(resume_type=outcome.resume_type, method="rules", confidence=outcome.confidence)
    if outcome.decision == "no_signal":
        return _DEFER
    # ambiguous
    if len(outcome.candidates) == 1:
        return RouteDecision(resume_type=outcome.candidates[0], method="rules", confidence=_SINGLE_CANDIDATE_CONFIDENCE)
    if not description:
        return _DEFER  # title-only ambiguity: defer until a JD arrives
    if budget.remaining <= 0:
        return _DEFER  # per-run LLM cap reached
    budget.remaining -= 1
    return resolve(outcome.candidates, title, description, llm_complete=llm_complete, config=config)


def route_new(conn: psycopg.Connection, *, config=None, llm_complete=None, max_llm_calls=None, reroute=False) -> dict:
    """Route unrouted, non-manual rows. With reroute=True, re-route all non-manual rows.
    Returns counts {rules, llm, deferred, manual_skipped}.

    Decisions are computed per row (so one bad row never aborts the run); the resulting
    UPDATEs are batched into a single pipelined executemany in one transaction, which
    collapses thousands of remote round-trips into one commit. A write failure propagates
    as an exception (the whole batch rolls back, retried idempotently next run) — the
    returned counts are only meaningful when the function returns normally.
    """
    cfg = config if config is not None else load_routing_config()
    do_llm = llm_complete if llm_complete is not None else llm_complete_default
    cap = max_llm_calls if max_llm_calls is not None else cfg.get("thresholds", {}).get("max_llm_calls_per_run", 200)
    budget = Budget(remaining=cap)

    if reroute:
        where = "route_method is distinct from 'manual'"
    else:
        where = "resume_type is null and route_method is distinct from 'manual'"

    rows = conn.execute(f"select id, title, description from jobs where {where}").fetchall()
    counts = {"rules": 0, "llm": 0, "deferred": 0, "manual_skipped": 0}
    updates: list[tuple] = []

    for job_id, title, description in rows:
        try:
            decision = route_one(title, description, cfg, llm_complete=do_llm, budget=budget)
        except Exception as exc:  # noqa: BLE001 - one bad row never aborts the run
            logger.warning("route: job %s failed: %s", job_id, exc)
            counts["deferred"] += 1
            continue
        if decision.method is None:
            counts["deferred"] += 1
            continue
        updates.append((decision.resume_type, decision.method, decision.confidence, job_id))
        counts[decision.method] += 1

    if updates:
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany(
                "update jobs set resume_type=%s, route_method=%s, route_confidence=%s, status='routed' where id=%s",
                updates,
            )
    counts["manual_skipped"] = conn.execute(
        "select count(*) from jobs where route_method = 'manual'"
    ).fetchone()[0]
    logger.info("route summary: %s (budget left=%d)", counts, budget.remaining)
    return counts


def set_manual(conn: psycopg.Connection, job_id, resume_type: str) -> None:
    """Operator override: pin a resume_type as manual. Validates the type."""
    if resume_type not in VALID_TYPES:
        raise ValueError(f"invalid resume_type {resume_type!r}; must be one of {VALID_TYPES}")
    with conn.transaction():
        cur = conn.execute(
            "update jobs set resume_type=%s, route_method='manual', route_confidence=1.0, status='routed' where id=%s",
            (resume_type, job_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"no job with id {job_id}")  # rolls back the (no-op) savepoint


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    with psycopg.connect(settings.database_url) as conn:
        if len(sys.argv) >= 2 and sys.argv[1] == "set":
            if len(sys.argv) != 4:
                sys.exit("usage: python -m jobmaxxing.route set <job_id> <resume_type>")
            set_manual(conn, sys.argv[2], sys.argv[3])
            print(f"set job {sys.argv[2]} -> {sys.argv[3]} (manual)")
        else:
            counts = route_new(conn)
            print(f"routed: {counts}")


if __name__ == "__main__":
    main()
