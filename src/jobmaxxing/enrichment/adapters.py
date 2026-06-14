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


class LeverAdapter:
    name = "lever"
    # jobs.lever.co/{site}/{uuid}  (an optional /apply suffix is ignored by the regex).
    _RE = re.compile(r"jobs\.lever\.co/([^/?#]+)/([0-9a-fA-F-]+)")

    @classmethod
    def matches(cls, url: str) -> bool:
        return cls._RE.search(url) is not None

    @classmethod
    def api_url(cls, url: str) -> str:
        m = cls._RE.search(url)
        site, jid = m.group(1), m.group(2)
        return f"https://api.lever.co/v0/postings/{site}/{jid}?mode=json"

    @classmethod
    def parse(cls, payload: dict, url: str) -> str | None:
        return payload.get("descriptionPlain") or None


class AshbyAdapter:
    name = "ashby"
    # jobs.ashbyhq.com/{org}/{postingUuid}  (optional /application suffix ignored).
    _RE = re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-fA-F-]+)")

    @classmethod
    def matches(cls, url: str) -> bool:
        return cls._RE.search(url) is not None

    @classmethod
    def api_url(cls, url: str) -> str:
        m = cls._RE.search(url)
        org = m.group(1)
        return f"https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true"

    @classmethod
    def parse(cls, payload: dict, url: str) -> str | None:
        m = cls._RE.search(url)
        posting_id = m.group(2)
        for posting in payload.get("jobs", []):
            if posting.get("id") == posting_id:
                return posting.get("descriptionPlain") or None
        return None  # posting no longer on the board -> permanent


class SmartRecruitersAdapter:
    name = "smartrecruiters"
    # jobs.smartrecruiters.com/{company}/{numeric postingId}
    _RE = re.compile(r"jobs\.smartrecruiters\.com/([^/?#]+)/(\d+)")

    @classmethod
    def matches(cls, url: str) -> bool:
        return cls._RE.search(url) is not None

    @classmethod
    def api_url(cls, url: str) -> str:
        m = cls._RE.search(url)
        company, posting_id = m.group(1), m.group(2)
        return f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{posting_id}"

    @classmethod
    def parse(cls, payload: dict, url: str) -> str | None:
        sections = payload.get("jobAd", {}).get("sections", {})
        parts = [
            sections.get(key, {}).get("text")
            for key in ("jobDescription", "qualifications")
        ]
        text = "\n".join(p for p in parts if p)
        return text or None


ADAPTERS = [GreenhouseAdapter, LeverAdapter, AshbyAdapter, SmartRecruitersAdapter]

# Coarse Postgres regex (case-insensitive ~*) used to keep the candidate query's LIMIT
# spent only on supported rows. adapter_for() is the precise per-row router/guard.
SUPPORTED_HOSTS_SQL = r"greenhouse\.io|lever\.co|ashbyhq\.com|smartrecruiters\.com"


def adapter_for(url: str):
    """Return the first adapter whose matches(url) is true, or None."""
    for adapter in ADAPTERS:
        if adapter.matches(url):
            return adapter
    return None
