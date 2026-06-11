import sys
from datetime import datetime, timezone

import psycopg

from .config import load_settings, load_watchlist
from .fetch import fetch_json
from .models import JobRecord
from .pipeline import ingest_records
from .sources.ats import parse_ashby, parse_greenhouse, parse_lever
from .sources.github_lists import parse_simplify_format

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


def _github_list_source(source: str, url: str):
    def fetch():
        return parse_simplify_format(fetch_json(url), source=source)
    return fetch


def _ats_source(company: str, ats: str, token: str):
    parser, url_tmpl = _ATS_PARSERS[ats]

    def fetch():
        return parser(fetch_json(url_tmpl.format(token=token)), company=company)
    return fetch


def build_sources() -> list[tuple[str, callable]]:
    sources: list[tuple[str, callable]] = []
    for source, url in GITHUB_LISTS:
        sources.append((source, _github_list_source(source, url)))
    for entry in load_watchlist():
        label = f"{entry['ats']}:{entry['token']}"
        sources.append((label, _ats_source(entry["company"], entry["ats"], entry["token"])))
    return sources


def run_sources(conn: psycopg.Connection, sources, now: datetime) -> dict:
    """Run each source in isolation. One failing source never blocks the others."""
    report: dict = {}
    for name, fetch in sources:
        try:
            records: list[JobRecord] = fetch()
            counts = ingest_records(conn, records, now=now)
            report[name] = {"status": "ok", **counts}
            print(f"[{name}] ok: {counts}")
        except Exception as exc:  # noqa: BLE001 - per-source isolation is the whole point
            report[name] = {"status": "failed", "error": str(exc)}
            print(f"[{name}] FAILED: {exc}", file=sys.stderr)
    return report


def main() -> None:
    settings = load_settings()
    now = datetime.now(timezone.utc)
    with psycopg.connect(settings.database_url) as conn:
        run_sources(conn, build_sources(), now=now)
    # Always exit 0: a failing source is logged, not fatal.
    sys.exit(0)


if __name__ == "__main__":
    main()
