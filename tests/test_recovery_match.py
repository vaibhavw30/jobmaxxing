from jobmaxxing.recovery.extract import JobPosting
from jobmaxxing.recovery.match import MatchResult, match_job


def _job(**kw):
    base = {"company": "Chegg", "title": "Computational Linguist", "url": "https://x.wd1.myworkdayjobs.com/j", "req_id": "JR012226"}
    base.update(kw)
    return base


def _llm_never(job, cand):
    raise AssertionError("llm_confirm must not be called")


def test_accept_on_reqid_without_llm():
    cand = JobPosting(description="d", title="Comp Linguist", company="Chegg", identifier="JR012226")
    r = match_job(_job(), cand, llm_confirm=_llm_never)
    assert r.accepted and r.reason == "reqid"


def test_accept_on_backlink_without_llm():
    cand = JobPosting(description="d", company="Chegg", url="https://x.wd1.myworkdayjobs.com/j")
    r = match_job(_job(req_id=None), cand, llm_confirm=_llm_never)
    assert r.accepted and r.reason == "backlink"


def test_reject_company_mismatch_without_llm():
    cand = JobPosting(description="d", title="Computational Linguist", company="TotallyOther Inc", identifier="ZZ")
    r = match_job(_job(req_id=None), cand, llm_confirm=_llm_never)
    assert not r.accepted and r.reason == "rejected:company"


def test_fuzzy_company_title_then_llm_confirm():
    cand = JobPosting(description="d", title="Computational Linguist (FTC)", company="Chegg Inc", identifier="ZZ")
    assert match_job(_job(req_id=None), cand, llm_confirm=lambda j, c: True).reason == "llm_confirmed"
    assert not match_job(_job(req_id=None), cand, llm_confirm=lambda j, c: False).accepted


def test_reject_title_dissimilar_without_llm():
    cand = JobPosting(description="d", title="Warehouse Forklift Operator", company="Chegg", identifier="ZZ")
    r = match_job(_job(req_id=None), cand, llm_confirm=_llm_never)
    assert not r.accepted and r.reason == "rejected:title"
