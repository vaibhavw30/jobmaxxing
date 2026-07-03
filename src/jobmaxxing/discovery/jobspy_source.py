"""JobSpy discovery source — a local, operator-run worker (residential IP; `discovery` extra).

parse_jobspy is a pure, defensive adapter (no pandas/network). Only _jobspy_scrape imports jobspy,
lazily, so this module imports fine without the extra and CI never touches JobSpy.
"""

import logging
from datetime import date, datetime, timezone

import psycopg
import yaml

from ..config import REPO_ROOT, load_settings
from ..models import JobRecord
from ..normalize import make_dedupe_key
from ..pipeline import ingest_records

logger = logging.getLogger(__name__)


def _clean_str(value):
    """A trimmed non-empty string, or None. Non-strings (incl. pandas NaN floats) -> None."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _coerce_dt(value):
    """Coerce JobSpy's date_posted to a tz-aware UTC datetime, or None (NaN/blank/unparseable)."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _location(row):
    loc = _clean_str(row.get("location"))
    if loc:
        return loc
    parts = [_clean_str(row.get(k)) for k in ("city", "state", "country")]
    joined = ", ".join(p for p in parts if p)
    return joined or None


def parse_jobspy(rows, *, site):
    """Normalize JobSpy rows (DataFrame.to_dict('records')) into JobRecords. Defensive: rows missing
    title/company/job_url are skipped (fail-soft). source = f'jobspy:{site}'."""
    source = f"jobspy:{site}"
    records = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        company = _clean_str(row.get("company"))
        title = _clean_str(row.get("title"))
        url = _clean_str(row.get("job_url"))
        if not company or not title or not url:
            continue
        records.append(JobRecord(
            source=source,
            company=company,
            title=title,
            url=url,
            external_id=url,
            location=_location(row),
            description=_clean_str(row.get("description")),
            posted_at=_coerce_dt(row.get("date_posted")),
            dedupe_key=make_dedupe_key(company, title),
        ))
    return records


def load_jobspy_config(path=None) -> dict:
    """Load config/jobspy.yaml (mirrors routing.config.load_routing_config). Missing file -> {}."""
    path = path or REPO_ROOT / "config" / "jobspy.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _slug(term: str) -> str:
    return term.strip().lower().replace(" ", "-")


def discover_jobspy(conn, *, scrape, config, now) -> dict:
    """Run each (site, search_term) via the injected scrape fn, parse + ingest. Fail-soft per search:
    a 429/network/parse error on one never blocks the rest. Returns a per-search report."""
    sites = config.get("sites", [])
    terms = config.get("search_terms", [])
    results_wanted = config.get("results_wanted", {})
    report = {}
    for site in sites:
        for term in terms:
            key = f"jobspy:{site}:{_slug(term)}"
            search = {
                "site": site,
                "term": term,
                "location": config.get("location"),
                "results_wanted": results_wanted.get(site, 50),
                "hours_old": config.get("hours_old"),
                "country_indeed": config.get("country_indeed"),
                "job_type": config.get("job_type"),
            }
            if site == "linkedin":
                search["linkedin_fetch_description"] = config.get("linkedin_fetch_description", False)
            try:
                rows = scrape(search)
                records = parse_jobspy(rows, site=site)
                counts = ingest_records(conn, records, now=now)
                report[key] = {"status": "ok", **counts}
            except Exception as exc:  # fail-soft: one search never blocks the others
                logger.warning("jobspy search failed [%s]: %s", key, exc)
                report[key] = {"status": "error", "error": str(exc)}
    return report


def _jobspy_scrape(search: dict) -> list[dict]:
    """The ONLY network/pandas code: call JobSpy and return list-of-dict rows. Lazily imports jobspy so
    the module loads without the `discovery` extra."""
    from jobspy import scrape_jobs

    kwargs = dict(
        site_name=[search["site"]],
        search_term=search["term"],
        location=search.get("location"),
        results_wanted=search.get("results_wanted", 50),
        job_type=search.get("job_type"),
    )
    if search.get("hours_old") is not None:
        kwargs["hours_old"] = search["hours_old"]
    if search.get("country_indeed"):
        kwargs["country_indeed"] = search["country_indeed"]
    if "linkedin_fetch_description" in search:
        kwargs["linkedin_fetch_description"] = search["linkedin_fetch_description"]
    df = scrape_jobs(**kwargs)
    if df is None or len(df) == 0:
        return []
    return df.to_dict("records")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    config = load_jobspy_config()
    with psycopg.connect(settings.database_url) as conn:
        report = discover_jobspy(conn, scrape=_jobspy_scrape, config=config,
                                 now=datetime.now(timezone.utc))
    ok = sum(1 for r in report.values() if r.get("status") == "ok")
    for key, res in report.items():
        logger.info("%s: %s", key, res)
    print(f"jobspy discovery: {ok}/{len(report)} searches ok")
