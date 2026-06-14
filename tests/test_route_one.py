from jobmaxxing.routing.route import route_one
from jobmaxxing.routing.types import Budget

CONFIG = {
    "weights": {"title": 3.0, "jd": 1.0},
    "thresholds": {"min_top_score": 1.0, "min_margin_ratio": 0.5, "jd_hits_cap": 5},
    "types": {
        "swe": {"definition": "x", "title_signals": ["software engineer"], "jd_signals": ["api"], "exclude_signals": []},
        "ai": {"definition": "x", "title_signals": ["ai engineer"], "jd_signals": ["llm"], "exclude_signals": []},
        "mle": {"definition": "x", "title_signals": ["ml engineer"], "jd_signals": ["training"], "exclude_signals": []},
    },
}

# Config with a higher min_top_score threshold so that a single weak JD match (score=1)
# falls below the threshold and ends up as ambiguous with a single candidate — the exact
# scenario that exercises the _SINGLE_CANDIDATE_CONFIDENCE path in route_one.
_CONFIG_HIGH_THRESHOLD = {
    "weights": {"title": 3.0, "jd": 1.0},
    "thresholds": {"min_top_score": 2.0, "min_margin_ratio": 0.5, "jd_hits_cap": 5},
    "types": {
        "swe": {"definition": "x", "title_signals": ["software engineer"], "jd_signals": ["api"], "exclude_signals": []},
        "ai": {"definition": "x", "title_signals": ["ai engineer"], "jd_signals": ["llm"], "exclude_signals": []},
        "mle": {"definition": "x", "title_signals": ["ml engineer"], "jd_signals": ["training"], "exclude_signals": []},
    },
}


def _llm_never(*a, **k):
    raise AssertionError("LLM must not be called")


def test_clear_title_routes_without_llm():
    b = Budget(remaining=5)
    d = route_one("Software Engineer Intern", "anything", CONFIG, llm_complete=_llm_never, budget=b)
    assert d.resume_type == "swe" and d.method == "rules"
    assert b.remaining == 5            # untouched


def test_ambiguous_with_jd_calls_llm_and_spends_budget():
    calls = []

    def fake_llm(task, messages, **kw):
        calls.append(1)
        return '{"type": "ai", "confidence": 0.7}'

    b = Budget(remaining=5)
    d = route_one("AI Engineer / ML Engineer Intern", "generic body", CONFIG, llm_complete=fake_llm, budget=b)
    assert d.method == "llm" and d.resume_type == "ai"
    assert len(calls) == 1 and b.remaining == 4


def test_ambiguous_title_only_defers_without_llm():
    b = Budget(remaining=5)
    d = route_one("AI Engineer / ML Engineer Intern", None, CONFIG, llm_complete=_llm_never, budget=b)
    assert d.resume_type is None and d.method is None     # defer
    assert b.remaining == 5


def test_no_signal_defers():
    b = Budget(remaining=5)
    d = route_one("Summer Intern", None, CONFIG, llm_complete=_llm_never, budget=b)
    assert d.resume_type is None and d.method is None


def test_budget_exhausted_defers_ambiguous_jd_row():
    b = Budget(remaining=0)
    d = route_one("AI Engineer / ML Engineer Intern", "generic body", CONFIG, llm_complete=_llm_never, budget=b)
    assert d.resume_type is None and d.method is None     # cap hit -> defer, no LLM


def test_single_candidate_ambiguity_routes_without_llm():
    # With min_top_score=2.0, a single 'api' hit (score=1.0) is below threshold.
    # "Summer Intern" has no title signal; only swe's jd_signal "api" appears in the
    # description -> swe is the sole positive JD type -> route_by_rules returns
    # ambiguous with candidates=["swe"]. route_one must route it directly, no LLM.
    b = Budget(remaining=5)
    d = route_one("Summer Intern", "we have an api and also some api", _CONFIG_HIGH_THRESHOLD, llm_complete=_llm_never, budget=b)
    assert d.resume_type == "swe" and d.method == "rules"
    assert b.remaining == 5            # no LLM spent


from jobmaxxing.routing.route import _looks_like_internship


def test_looks_like_internship_true_cases():
    for title in ("Software Engineering Intern", "ML Co-op", "Data Apprentice",
                  "New Grad SWE", "Campus Analyst", "Student Researcher"):
        assert _looks_like_internship(title) is True


def test_looks_like_internship_false_cases():
    for title in ("Senior Director, Finance", "Staff Software Engineer", None, ""):
        assert _looks_like_internship(title) is False
