"""Decide whether a recovered JobPosting is the SAME job — deterministic first, LLM-confirm fuzzy."""

import re
from dataclasses import dataclass

from .extract import JobPosting


@dataclass
class MatchResult:
    accepted: bool
    reason: str   # "reqid" | "backlink" | "llm_confirmed" | "rejected:<why>"


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _company_matches(a, b) -> bool:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    at, bt = set(a.split()), set(b.split())
    return bool(at & bt) and (a in b or b in a or len(at & bt) / max(len(at), len(bt)) >= 0.5)


def _title_similar(a, b) -> bool:
    at, bt = set(_norm(a).split()), set(_norm(b).split())
    return bool(at) and bool(bt) and len(at & bt) / len(at | bt) >= 0.4   # Jaccard


def match_job(job: dict, cand: JobPosting, *, llm_confirm) -> MatchResult:
    """job = {company, title, url, req_id}. Accept deterministically on req-id/back-link; else
    require fuzzy company+title and an LLM 'same posting?' confirm. Prefer a safe miss."""
    hay = " ".join(filter(None, [cand.description, cand.identifier, cand.url, cand.source_url, cand.title]))
    req = job.get("req_id")
    if req and req.lower() in hay.lower():
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
