import pytest

from jobmaxxing.llm.client import LLMUnavailable
from jobmaxxing.routing.tiebreaker import resolve

CONFIG = {
    "weights": {"title": 3.0, "jd": 1.0},
    "thresholds": {"jd_hits_cap": 5},
    "types": {
        "ai": {"definition": "Applied generative AI.", "jd_signals": ["llm", "rag"], "exclude_signals": []},
        "mle": {"definition": "ML engineering.", "jd_signals": ["training"], "exclude_signals": []},
    },
}


def test_resolve_uses_valid_llm_answer():
    def fake_llm(task, messages, **kw):
        return '{"type": "ai", "confidence": 0.77}'

    d = resolve(["ai", "mle"], "AI/ML Intern", "some jd", llm_complete=fake_llm, config=CONFIG)
    assert d.resume_type == "ai" and d.method == "llm" and d.confidence == 0.77


def test_resolve_falls_back_on_invalid_answer():
    def fake_llm(task, messages, **kw):
        return '{"type": "fdse", "confidence": 0.9}'   # out of candidate set

    # jd clearly favors ai (2 signals) over mle (0)
    d = resolve(["ai", "mle"], "AI/ML Intern", "llm and rag", llm_complete=fake_llm, config=CONFIG)
    assert d.resume_type == "ai" and d.method == "rules" and d.confidence < 0.5


def test_resolve_falls_back_when_llm_unavailable():
    def fake_llm(task, messages, **kw):
        raise LLMUnavailable("down")

    d = resolve(["ai", "mle"], "AI/ML Intern", "training only", llm_complete=fake_llm, config=CONFIG)
    assert d.resume_type == "mle" and d.method == "rules"   # jd favors mle here


def test_resolve_empty_candidates_defers():
    d = resolve([], "x", "y", llm_complete=lambda *a, **k: "{}", config=CONFIG)
    assert d.resume_type is None and d.method is None   # nothing to decide -> defer


def test_resolve_propagates_non_llmunavailable_errors():
    # a non-LLMUnavailable error is NOT a "fall back to rules" case; it must propagate so
    # the batch loop (route_new) can isolate and defer that row.
    def boom(task, messages, **kw):
        raise RuntimeError("unexpected sdk failure")

    with pytest.raises(RuntimeError):
        resolve(["ai", "mle"], "x", "y", llm_complete=boom, config=CONFIG)
