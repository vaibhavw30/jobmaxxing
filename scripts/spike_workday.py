"""Spike: measure Workday tiered-fetch yield against live jobs from the DB.

Run locally with the headless extra installed:
    uv run --extra headless python scripts/spike_workday.py [N]
Prints a per-outcome tally so we can confirm the headless approach's real hit rate.
"""

import os
import sys
from collections import Counter

import psycopg
from dotenv import load_dotenv

from jobmaxxing.enrichment.playwright_fetcher import PlaywrightFetcher
from jobmaxxing.enrichment.workday import fetch_workday_one


def main(n: int) -> None:
    load_dotenv()
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        rows = conn.execute(
            "select id, url from jobs where url ~* 'myworkdayjobs\\.com' "
            "and coalesce(description,'')='' order by scraped_at desc limit %s",
            (n,),
        ).fetchall()
    fetcher = PlaywrightFetcher()
    tally: Counter = Counter()
    try:
        for job_id, url in rows:
            out = fetch_workday_one(job_id, url, fetcher)
            tally[out.kind] += 1
            print(f"{out.kind:9} {url[:70]}")
    finally:
        fetcher.close()
    print("\nyield:", dict(tally), f"(of {len(rows)})")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 30)
