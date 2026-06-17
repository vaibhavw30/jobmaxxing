import logging

import psycopg
from mcp.server.fastmcp import FastMCP

from ..config import load_settings
from ..llm.client import complete as llm_complete
from ..tailoring.latex import compile_pdf
from ..tailoring.storage import make_store
from . import tools

logger = logging.getLogger(__name__)
mcp = FastMCP("jobmaxxing")


def _conn():
    return psycopg.connect(load_settings().database_url)


def _store():
    """Build the artifact store from the environment (RESUME_STORE_DIR or S3_BUCKET)."""
    return make_store()


@mcp.tool()
def query_jobs(status: str | None = None, resume_type: str | None = None,
               company: str | None = None, jd_source: str | None = None,
               since_days: int | None = None, limit: int = 50) -> list[dict]:
    """List postings from the feed, newest first, filtered by status/resume_type/company/jd_source/recency.
    company is a case-insensitive substring; jd_source filters how the JD was obtained ('recovered' for
    find-elsewhere JDs to spot-check); since_days bounds scraped_at. limit defaults to 50, max 200."""
    with _conn() as conn:
        return tools.query_jobs(conn, status=status, resume_type=resume_type, company=company,
                                jd_source=jd_source, since_days=since_days, limit=limit)


@mcp.tool()
def preview_route(job_id: str, rerun: bool = False) -> dict:
    """Show a job's stored resume-type route. With rerun=true, ALSO returns a fresh routing
    decision computed now (read-only — it is NOT persisted; use set_route to change the route)."""
    with _conn() as conn:
        return tools.preview_route(conn, job_id, rerun=rerun)


@mcp.tool()
def set_route(job_id: str, resume_type: str) -> dict:
    """Manually override a job's resume type (route_method=manual; never auto-re-routed)."""
    with _conn() as conn:
        return tools.set_route(conn, job_id, resume_type)


@mcp.tool()
def approve(job_id: str) -> dict:
    """Gate a job for tailoring (status -> approved_for_tailoring). Use THIS, not set_status,
    to approve for tailoring — it is the dedicated, precondition-aware gate."""
    with _conn() as conn:
        return tools.approve(conn, job_id)


@mcp.tool()
def tailor_job(job_id: str) -> dict:
    """Run the full tailoring loop for an approved job and return the review summary. SLOW —
    typically 30-120s (several LLM calls + pdflatex); confirm with the user before calling.
    The job must already be approved_for_tailoring (use the approve tool first)."""
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
    """Set a job's funnel status to one of: new, routed, approved_for_tailoring, tailored,
    reviewed, applied, rejected. This is the human gate for tailored->applied|rejected. To
    approve a job FOR TAILORING, prefer the dedicated `approve` tool over this."""
    with _conn() as conn:
        return tools.set_status(conn, job_id, status)


@mcp.tool()
def nightly_queue(limit: int = 50) -> list[dict]:
    """Tonight's manual-capture worklist: relevant, still-JD-less jobs that both the headless Workday
    worker and find-elsewhere gave up on. Open each in your own browser to read the JD, then call
    set_description. limit defaults to 50, max 200."""
    with _conn() as conn:
        return tools.nightly_queue(conn, limit=limit)


@mcp.tool()
def set_description(job_id: str, text: str) -> dict:
    """Ingest a job description you obtained for a job_id (e.g. pasted from the posting page). Stores
    it (jd_source='manual') and resets routing so the next routing run classifies it with the JD."""
    with _conn() as conn:
        return tools.set_description(conn, job_id, text)


@mcp.tool()
def reject_recovered(job_id: str) -> dict:
    """Discard a wrong auto-recovered JD (jd_source='recovered'): clears it and returns the job to the
    nightly_queue for manual capture. Errors if the job's JD was not auto-recovered."""
    with _conn() as conn:
        return tools.reject_recovered(conn, job_id)


@mcp.tool()
def sync_sheet() -> dict:
    """Sync the Google decision sheet both ways: apply your interested/applied marks to the funnel,
    then refresh the sheet with the latest routed jobs. Returns the counts."""
    from ..sheets.client import GspreadClient
    with _conn() as conn:
        return tools.sync_sheet(conn, client=GspreadClient())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    mcp.run()
