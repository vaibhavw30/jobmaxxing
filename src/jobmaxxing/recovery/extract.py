"""Extract a schema.org JobPosting (and the Workday req-id) for find-elsewhere recovery."""

import json
import re
from dataclasses import dataclass

_LD = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.DOTALL)
# Trailing slug token after the last '_': e.g. _JR012226, _R-1289, _REQ_0000068406-1
_REQID = re.compile(r"_([A-Za-z]*[-_]?\d[\w-]*)$")


@dataclass
class JobPosting:
    description: str                 # HTML
    title: str | None = None
    company: str | None = None       # hiringOrganization.name (or the string form)
    identifier: str | None = None    # identifier.value (or the string form)
    url: str | None = None           # canonical posting URL
    source_url: str | None = None    # the page we fetched it from


def workday_req_id(url: str) -> str | None:
    m = _REQID.search(url.split("?")[0].rstrip("/"))
    return m.group(1) if m else None


def _text(v, prefer_value: bool = False):
    """hiringOrganization / identifier may be a string OR an object — normalize to a string.

    When prefer_value=True (identifier field), PropertyValue.value is the req-id token;
    otherwise name is primary (hiringOrganization -> Organization.name).
    """
    if isinstance(v, dict):
        if prefer_value:
            return v.get("value") or v.get("name")
        return v.get("name") or v.get("value")
    return v if isinstance(v, str) else None


def extract_job_posting(html_text: str, *, source_url: str | None = None) -> JobPosting | None:
    """Return the first JSON-LD JobPosting with a non-empty description, or None."""
    for block in _LD.findall(html_text):
        try:
            data = json.loads(block.strip())
        except (ValueError, TypeError):
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
        for n in (nodes if isinstance(nodes, list) else [nodes]):
            if isinstance(n, dict) and n.get("@type") == "JobPosting" and n.get("description"):
                return JobPosting(
                    description=n["description"],
                    title=n.get("title"),
                    company=_text(n.get("hiringOrganization")),
                    identifier=_text(n.get("identifier"), prefer_value=True),
                    url=n.get("url"),
                    source_url=source_url,
                )
    return None
