import json
import logging
import sys

import psycopg
from psycopg.types.json import Json

from ..config import load_settings
from ..llm.client import complete as llm_complete
from .diffing import unified_diff
from .latex import compile_pdf, enforce_one_page
from .passes import apply_critique, build_tailored, critique_resume, shrink_to_one_page
from .rubric import load_rubric
from .scorer import delta, score
from .storage import make_store

logger = logging.getLogger(__name__)


def tailor_job(conn, job_id, *, store, complete, compile_fn, rubric_loader=load_rubric) -> dict:
    """Run the two-pass tailoring loop for one approved job. All boundaries injected."""
    row = conn.execute(
        "select description, resume_type, status from jobs where id=%s", (job_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no job with id {job_id}")
    jd, resume_type, status = row
    if status != "approved_for_tailoring":
        raise ValueError(f"job {job_id} is not approved_for_tailoring (status={status!r})")

    base_tex = store.get_base_resume(resume_type)
    rubric = rubric_loader(resume_type)

    before = score(base_tex, jd or "", rubric, complete=complete)     # Pass 0
    tailored = build_tailored(base_tex, jd or "", complete=complete)   # Pass 1
    critique = critique_resume(tailored, jd or "", complete=complete)  # Pass 2a
    patched = apply_critique(tailored, critique, jd or "", complete=complete)  # Pass 2b

    one_page = enforce_one_page(                                       # Pass 3
        patched,
        compile_fn=compile_fn,
        shrink_fn=lambda tex, pages: shrink_to_one_page(tex, pages, complete=complete),
    )
    final_tex = one_page.tex
    after = score(final_tex, jd or "", rubric, complete=complete)     # Pass 4

    review = {
        "score_before": before,
        "score_after": after,
        "delta": delta(before, after),
        "weaknesses": critique["weaknesses"],
        "missing_keywords": critique["missing_keywords"],
        "page_count": one_page.page_count,
        "retries": one_page.retries,
        "fit": one_page.fit,
    }
    diff = unified_diff(base_tex, final_tex)

    store.put_artifact(job_id, "tailored.tex", final_tex.encode("utf-8"))
    store.put_artifact(job_id, "tailored.pdf", one_page.pdf_bytes)
    store.put_artifact(job_id, "review.json", json.dumps(review, indent=2).encode("utf-8"))
    store.put_artifact(job_id, "diff.txt", diff.encode("utf-8"))

    with conn.transaction():
        conn.execute(
            "update jobs set score_before=%s, score_after=%s, artifact_prefix=%s, status='tailored' where id=%s",
            (Json(before), Json(after), store.artifact_prefix(job_id), job_id),
        )
    # A résumé that never fit one page (shrink retries exhausted) is a quality event, not routine.
    log = logger.warning if not one_page.fit else logger.info
    log("tailored job %s: delta=%s fit=%s", job_id, review["delta"], one_page.fit)
    return review


def approve(conn, job_id) -> None:
    """Operator gate: mark a job approved_for_tailoring. Re-approving an already-tailored
    job is allowed (re-tailoring) but logged, so it is never a silent overwrite."""
    row = conn.execute("select status from jobs where id=%s", (job_id,)).fetchone()
    if row is None:
        raise ValueError(f"no job with id {job_id}")
    if row[0] == "tailored":
        logger.warning("re-approving already-tailored job %s for re-tailoring", job_id)
    with conn.transaction():
        conn.execute("update jobs set status='approved_for_tailoring' where id=%s", (job_id,))


def _print_review(store, job_id) -> None:
    # review is stored as an artifact; re-fetch is store-specific, so point the operator at it.
    print(f"review at: {store.artifact_prefix(job_id)}review.json")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    try:
        store = make_store()
    except RuntimeError as exc:
        sys.exit(str(exc))
    with psycopg.connect(settings.database_url) as conn:
        if len(sys.argv) >= 2 and sys.argv[1] == "approve":
            if len(sys.argv) != 3:
                sys.exit("usage: python -m jobmaxxing.tailor approve <job_id>")
            approve(conn, sys.argv[2])
            print(f"approved {sys.argv[2]} for tailoring")
        elif len(sys.argv) >= 2 and sys.argv[1] == "review":
            if len(sys.argv) != 3:
                sys.exit("usage: python -m jobmaxxing.tailor review <job_id>")
            _print_review(store, sys.argv[2])
        elif len(sys.argv) == 2:
            review = tailor_job(conn, sys.argv[1], store=store, complete=llm_complete, compile_fn=compile_pdf)
            print(f"tailored {sys.argv[1]}: {review['delta']}")
        else:
            sys.exit("usage: python -m jobmaxxing.tailor [approve|review] <job_id>")


if __name__ == "__main__":
    main()
