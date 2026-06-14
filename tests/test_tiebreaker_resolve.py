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


from jobmaxxing.routing.tiebreaker import resolve_title_only

_TT_CONFIG = {
    "types": {
        "ai": {"definition": "AI engineering"},
        "mle": {"definition": "ML engineering"},
    }
}


def test_resolve_title_only_caps_confidence_and_tags_method():
    def fake_llm(task, messages, **kw):
        return '{"type": "ai", "confidence": 0.95}'
    d = resolve_title_only(["ai", "mle"], "AI / ML Engineer Intern", llm_complete=fake_llm, config=_TT_CONFIG)
    assert d.resume_type == "ai"
    assert d.method == "llm_title"
    assert d.confidence == 0.4          # capped at _TITLE_ROUTE_CONFIDENCE even though reply said 0.95


def test_resolve_title_only_defers_when_llm_unavailable():
    def fake_llm(task, messages, **kw):
        raise LLMUnavailable("no provider")
    d = resolve_title_only(["ai", "mle"], "AI / ML Intern", llm_complete=fake_llm, config=_TT_CONFIG)
    assert d.method is None             # defer -> retry next run


def test_resolve_title_only_defers_on_unparseable_reply():
    def fake_llm(task, messages, **kw):
        return "not json at all"
    d = resolve_title_only(["ai", "mle"], "AI / ML Intern", llm_complete=fake_llm, config=_TT_CONFIG)
    assert d.method is None


from jobmaxxing.routing.tiebreaker import build_classify_messages, classify_title

_FULL_CONFIG = {
    "types": {t: {"definition": f"{t} work"} for t in
              ("quant-trader", "quant-dev", "mle", "swe", "fdse", "ai", "robotics", "av")}
}


def test_build_classify_messages_lists_all_types_plus_none():
    msgs = build_classify_messages("ML Intern", _FULL_CONFIG)
    system = msgs[0]["content"]
    for t in ("swe", "mle", "av", "none"):
        assert t in system
    assert "ML Intern" in msgs[1]["content"]


def test_classify_title_returns_llm_title_for_a_type():
    def fake_llm(task, messages, **kw):
        return '{"type": "mle", "confidence": 0.9}'
    d = classify_title("Machine Learning Co-op", llm_complete=fake_llm, config=_FULL_CONFIG)
    assert d.resume_type == "mle" and d.method == "llm_title" and d.confidence == 0.4


def test_classify_title_none_is_not_target():
    def fake_llm(task, messages, **kw):
        return '{"type": "none", "confidence": 0.9}'
    d = classify_title("Warehouse Associate", llm_complete=fake_llm, config=_FULL_CONFIG)
    assert d.resume_type is None and d.method == "not_target"


def test_classify_title_defers_when_llm_unavailable():
    def fake_llm(task, messages, **kw):
        raise LLMUnavailable("no provider")
    d = classify_title("Some Intern", llm_complete=fake_llm, config=_FULL_CONFIG)
    assert d.method is None
