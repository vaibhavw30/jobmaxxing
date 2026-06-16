import logging
import sys
from datetime import datetime, timezone

import psycopg

from .config import load_settings, load_watchlist
from .fetch import fetch_json
from .models import JobRecord
from .normalize import upcoming_terms
from .pipeline import ingest_records
from .sources.ats import parse_ashby, parse_greenhouse, parse_lever
from .sources.github_lists import parse_simplify_format

logger = logging.getLogger(__name__)

# Simplify-format curated lists (raw listings.json URLs).
GITHUB_LISTS = [
    ("github:simplify", "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json"),
    ("github:vanshb03", "https://raw.githubusercontent.com/vanshb03/Summer2026-Internships/dev/.github/scripts/listings.json"),
    ("github:pitt-csc", "https://raw.githubusercontent.com/pittcsc/Summer2026-Internships/dev/.github/scripts/listings.json"),
]

_ATS_PARSERS = {
    "greenhouse": (parse_greenhouse, "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"),
    "lever": (parse_lever, "https://api.lever.co/v0/postings/{token}?mode=json"),
    "ashby": (parse_ashby, "https://api.ashbyhq.com/posting-api/job-board/{token}"),
}


def _github_list_source(source: str, url: str, allowed_terms):
    def fetch():
        return parse_simplify_format(fetch_json(url), source=source, allowed_terms=allowed_terms)
    return fetch


def _ats_source(company: str, ats: str, token: str):
    parser, url_tmpl = _ATS_PARSERS[ats]

    def fetch():
        return parser(fetch_json(url_tmpl.format(token=token)), company=company)
    return fetch


def build_sources(
    watchlist: list[dict] | None = None, *, allowed_terms: set[tuple[str, int]] | None = None
) -> list[tuple[str, object]]:
    """Assemble (name, fetch_callable) pairs: the GitHub lists plus valid watchlist ATS
    entries. Malformed watchlist entries (not a mapping, missing keys, or unknown ATS)
    are skipped with a warning so one bad config line can never abort the whole run.
    `watchlist` is injectable for testing; defaults to load_watchlist().
    ``allowed_terms`` (a set of (season, year) pairs) is forwarded to the Simplify-format
    parser to drop off-window postings at ingest time; None keeps all postings."""
    sources: list[tuple[str, object]] = []
    for source, url in GITHUB_LISTS:
        sources.append((source, _github_list_source(source, url, allowed_terms)))

    entries = load_watchlist() if watchlist is None else watchlist
    for entry in entries:
        if not isinstance(entry, dict):
            logger.warning("skipping malformed watchlist entry (not a mapping): %r", entry)
            continue
        company = entry.get("company")
        ats = entry.get("ats")
        token = entry.get("token")
        if not company or not token or ats not in _ATS_PARSERS:
            logger.warning("skipping invalid watchlist entry: %r", entry)
            continue
        sources.append((f"{company}:{ats}:{token}", _ats_source(company, ats, token)))
    return sources


def run_sources(conn: psycopg.Connection, sources, now: datetime) -> dict:
    """Run each source in isolation. One failing source never blocks the others."""
    report: dict = {}
    for name, fetch in sources:
        try:
            records: list[JobRecord] = fetch()
            counts = ingest_records(conn, records, now=now)
            report[name] = {"status": "ok", **counts}
            logger.info("[%s] ok: %s", name, counts)
        except Exception as exc:  # noqa: BLE001 - per-source isolation is the whole point
            report[name] = {"status": "failed", "error": str(exc)}
            logger.warning("[%s] FAILED: %s", name, exc)

    ok = [r for r in report.values() if r["status"] == "ok"]
    failed = [name for name, r in report.items() if r["status"] == "failed"]
    logger.info(
        "run summary: %d ok, %d failed; inserted=%d merged=%d skipped_old=%d; failed_sources=%s",
        len(ok),
        len(failed),
        sum(r.get("inserted", 0) for r in ok),
        sum(r.get("merged", 0) for r in ok),
        sum(r.get("skipped_old", 0) for r in ok),
        failed,
    )
    return report


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = load_settings()
    now = datetime.now(timezone.utc)
    allowed_terms = upcoming_terms(now.date())
    with psycopg.connect(settings.database_url) as conn:
        run_sources(conn, build_sources(allowed_terms=allowed_terms), now=now)
    # Per-source failures are isolated and logged, so a partial source failure still exits 0.
    # A DB/config error (bad DATABASE_URL, unreachable DB) is intentionally NOT swallowed:
    # it propagates and fails the run loudly, because that's an operator setup error, not a
    # transient source issue.
    sys.exit(0)


if __name__ == "__main__":
    main()
