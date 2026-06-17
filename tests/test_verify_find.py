import types

import jobmaxxing.verification.find as find
from jobmaxxing.verification.find import find_alternative_url


def _cand(url):
    return types.SimpleNamespace(url=url, source_url=url, company="Acme", title="SWE Intern")


def test_returns_first_confidently_matched_url(monkeypatch):
    monkeypatch.setattr(find, "extract_job_posting", lambda html, source_url=None: _cand(source_url))
    monkeypatch.setattr(find, "match_job",
                        lambda job, cand, llm_confirm: types.SimpleNamespace(accepted=cand.url == "https://r2"))
    out = find_alternative_url(
        "Acme", "SWE Intern", "https://dead",
        searcher=lambda q, *, fetch_text: ["https://r1", "https://r2"],
        fetcher=lambda url: "<html>",
        llm_confirm=lambda job, cand: True,
    )
    assert out == "https://r2"


def test_returns_none_when_nothing_matches(monkeypatch):
    monkeypatch.setattr(find, "extract_job_posting", lambda html, source_url=None: _cand(source_url))
    monkeypatch.setattr(find, "match_job", lambda job, cand, llm_confirm: types.SimpleNamespace(accepted=False))
    out = find_alternative_url(
        "Acme", "SWE Intern", "https://dead",
        searcher=lambda q, *, fetch_text: ["https://r1"],
        fetcher=lambda url: "<html>",
        llm_confirm=lambda job, cand: True,
    )
    assert out is None


def test_skips_unparseable_candidate(monkeypatch):
    def extract(html, source_url=None):
        if source_url == "https://bad":
            return None
        return _cand(source_url)
    monkeypatch.setattr(find, "extract_job_posting", extract)
    monkeypatch.setattr(find, "match_job", lambda job, cand, llm_confirm: types.SimpleNamespace(accepted=True))
    out = find_alternative_url(
        "Acme", "SWE Intern", "https://dead",
        searcher=lambda q, *, fetch_text: ["https://bad", "https://good"],
        fetcher=lambda url: "<html>",
        llm_confirm=lambda job, cand: True,
    )
    assert out == "https://good"


def test_search_failure_returns_none(monkeypatch):
    def boom(q, *, fetch_text):
        raise RuntimeError("ddg blocked")
    out = find_alternative_url("Acme", "SWE Intern", "https://dead",
                               searcher=boom, fetcher=lambda url: "<html>", llm_confirm=lambda j, c: True)
    assert out is None
