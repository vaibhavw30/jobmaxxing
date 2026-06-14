"""Decide whether a recovered JobPosting is the SAME job — deterministic first, LLM-confirm fuzzy."""

import re
from dataclasses import dataclass

from .extract import JobPosting


@dataclass
class MatchResult:
    accepted: bool
    reason: str   # "reqid" | "backlink" | "llm_confirmed" | "rejected:<why>"


# Corporate suffixes shared by unrelated companies ("Acme Inc" vs "Boring Inc") — ignored when
# matching so a common suffix alone can't pass the company check (and waste an LLM call).
_COMPANY_STOPWORDS = {"inc", "corp", "llc", "ltd", "co", "the", "company", "group", "holdings", "plc"}


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _company_matches(a, b) -> bool:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    at = set(a.split()) - _COMPANY_STOPWORDS
    bt = set(b.split()) - _COMPANY_STOPWORDS
    if not at or not bt:
        return False
    return bool(at & bt) and (a in b or b in a or len(at & bt) / max(len(at), len(bt)) >= 0.5)


def _reqid_in(req: str, hay: str) -> bool:
    """Whole-token match: a short req-id like 'R-9' must NOT match 'R-99'/'R-9000' inside a
    different posting's text. '_' is allowed as a boundary (URLs separate the slug token with it)."""
    return re.search(r"(?<![A-Za-z0-9-])" + re.escape(req) + r"(?![A-Za-z0-9-])", hay, re.IGNORECASE) is not None


def _title_similar(a, b) -> bool:
    at, bt = set(_norm(a).split()), set(_norm(b).split())
    return bool(at) and bool(bt) and len(at & bt) / len(at | bt) >= 0.4   # Jaccard


def match_job(job: dict, cand: JobPosting, *, llm_confirm) -> MatchResult:
    """job = {company, title, url, req_id}. Accept deterministically on req-id/back-link; else
    require fuzzy company+title and an LLM 'same posting?' confirm. Prefer a safe miss."""
    hay = " ".join(filter(None, [cand.description, cand.identifier, cand.url, cand.source_url, cand.title]))
    req = job.get("req_id")
    if req and _reqid_in(req, hay):
        return MatchResult(True, "reqid")
    if job.get("url") and job["url"] in hay:
        return MatchResult(True, "backlink")
    if not _company_matches(job.get("company"), cand.company):
        return MatchResult(False, "rejected:company")
    if not _title_similar(job.get("title"), cand.title):
        return MatchResult(False, "rejected:title")
    if llm_confirm(job, cand):
        return MatchResult(True, "llm_confirmed")
    return MatchResult(False, "rejected:llm")
