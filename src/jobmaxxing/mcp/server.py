import logging
import os

import psycopg
from mcp.server.fastmcp import FastMCP

from ..config import load_settings
from ..llm.client import complete as llm_complete
from ..tailoring.latex import compile_pdf
from ..tailoring.storage import S3Store
from . import tools

logger = logging.getLogger(__name__)
mcp = FastMCP("jobmaxxing")


def _conn():
    return psycopg.connect(load_settings().database_url)


def _store() -> S3Store:
    bucket = os.environ.get("S3_BUCKET")
    if not bucket:
        raise RuntimeError("S3_BUCKET is not set (see README / .env.example)")
    return S3Store(bucket)


@mcp.tool()
def query_jobs(status: str | None = None, resume_type: str | None = None,
               company: str | None = None, since_days: int | None = None,
               limit: int = 50) -> list[dict]:
    """List postings from the feed (newest first), filtered by status/type/company/recency. limit capped at 200."""
    with _conn() as conn:
        return tools.query_jobs(conn, status=status, resume_type=resume_type,
                                company=company, since_days=since_days, limit=limit)


@mcp.tool()
def preview_route(job_id: str, rerun: bool = False) -> dict:
    """Show a job's stored route; with rerun=true, also what the router would assign now (not saved)."""
    with _conn() as conn:
        return tools.preview_route(conn, job_id, rerun=rerun)


@mcp.tool()
def set_route(job_id: str, resume_type: str) -> dict:
    """Manually override a job's resume type (route_method=manual; never auto-re-routed)."""
    with _conn() as conn:
        return tools.set_route(conn, job_id, resume_type)


@mcp.tool()
def approve(job_id: str) -> dict:
    """Approve a job for tailoring (status -> approved_for_tailoring)."""
    with _conn() as conn:
        return tools.approve(conn, job_id)


@mcp.tool()
def tailor_job(job_id: str) -> dict:
    """Run the full tailoring loop for an approved job (slow: LLM + pdflatex). Returns the review summary."""
    store = _store()
    with _conn() as conn:
        return tools.tailor(conn, job_id, store=store, complete=llm_complete, compile_fn=compile_pdf)


@mcp.tool()
def get_review(job_id: str) -> dict:
    """Fetch a tailored job's review.json + diff.txt from storage."""
    # get_review needs only the store (no DB), so no _conn() here — store mirrors conn's role.
    return tools.get_review(_store(), job_id)


@mcp.tool()
def set_status(job_id: str, status: str) -> dict:
    """Move a job through the funnel (new/routed/approved_for_tailoring/tailored/reviewed/applied/rejected)."""
    with _conn() as conn:
        return tools.set_status(conn, job_id, status)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    mcp.run()


if __name__ == "__main__":
    main()
