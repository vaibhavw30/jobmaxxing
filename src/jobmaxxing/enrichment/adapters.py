"""URL -> per-job JSON API adapters for the clean-API ATS sources.

Each adapter is a stateless class with three classmethods:
  matches(url) -> bool        host/path test
  api_url(url) -> str         translate the human page URL to the JSON endpoint
  parse(payload, url) -> str | None   extract the description, or None if absent
A None parse result means the posting is gone/unparseable -> permanent failure.
"""

import html
import re


class GreenhouseAdapter:
    name = "greenhouse"
    # Both the new (job-boards) and classic (boards) hosts; {token}/jobs/{numeric id}.
    _RE = re.compile(r"(?:job-boards|boards)\.greenhouse\.io/([^/?#]+)/jobs/(\d+)")

    @classmethod
    def matches(cls, url: str) -> bool:
        return cls._RE.search(url) is not None

    @classmethod
    def api_url(cls, url: str) -> str:
        m = cls._RE.search(url)
        token, jid = m.group(1), m.group(2)
        return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{jid}?content=true"

    @classmethod
    def parse(cls, payload: dict, url: str) -> str | None:
        content = payload.get("content")
        return html.unescape(content) if content else None


ADAPTERS = [GreenhouseAdapter]

# Coarse Postgres regex (case-insensitive ~*) used to keep the candidate query's LIMIT
# spent only on supported rows. adapter_for() is the precise per-row router/guard.
SUPPORTED_HOSTS_SQL = r"greenhouse\.io|lever\.co|ashbyhq\.com|smartrecruiters\.com"


def adapter_for(url: str):
    """Return the first adapter whose matches(url) is true, or None."""
    for adapter in ADAPTERS:
        if adapter.matches(url):
            return adapter
    return None
