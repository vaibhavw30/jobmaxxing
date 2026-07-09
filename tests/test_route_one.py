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


def _ambiguous_no_jd():
    # "ai engineer ml engineer" matches BOTH ai and mle title_signals -> 2 candidates, no JD
    return ("AI Engineer / ML Engineer Intern", None)


def test_exhausted_ambiguous_no_jd_title_routes_via_llm_title():
    title, desc = _ambiguous_no_jd()
    tb = Budget(remaining=5)
    jd_b = Budget(remaining=5)

    def fake_llm(task, messages, **kw):
        return '{"type": "ai", "confidence": 0.9}'

    d = route_one(title, desc, CONFIG, llm_complete=fake_llm, budget=jd_b,
                  exhausted=True, title_budget=tb)
    assert d.method == "llm_title" and d.resume_type == "ai" and d.confidence == 0.4
    assert tb.remaining == 4 and jd_b.remaining == 5        # spent the TITLE budget, not the JD budget


def test_not_exhausted_ambiguous_no_jd_still_defers():
    title, desc = _ambiguous_no_jd()
    d = route_one(title, desc, CONFIG, llm_complete=_llm_never, budget=Budget(5),
                  exhausted=False, title_budget=Budget(5))
    assert d.method is None                                 # unchanged behavior when not exhausted


def test_exhausted_but_no_title_budget_defers():
    title, desc = _ambiguous_no_jd()
    d = route_one(title, desc, CONFIG, llm_complete=_llm_never, budget=Budget(5),
                  exhausted=True, title_budget=Budget(0))
    assert d.method is None


def test_exhausted_no_signal_internship_open_classifies():
    # "Robotics Perception Intern" matches no title_signal in CONFIG (swe/ai/mle only) -> no_signal
    def fake_llm(task, messages, **kw):
        return '{"type": "ai", "confidence": 0.8}'
    tb = Budget(remaining=2)
    d = route_one("Robotics Perception Intern", None, CONFIG, llm_complete=fake_llm,
                  budget=Budget(5), exhausted=True, title_budget=tb)
    assert d.method == "llm_title" and tb.remaining == 1


def test_exhausted_no_signal_non_internship_is_not_target_without_llm():
    d = route_one("Senior Director of Finance", None, CONFIG, llm_complete=_llm_never,
                  budget=Budget(5), exhausted=True, title_budget=Budget(2))
    assert d.method == "not_target" and d.resume_type is None


def test_has_jd_ambiguous_still_uses_normal_llm_path():
    # regression: the JD-bearing path is unchanged (method='llm', spends the JD budget)
    # "generic body" has no ai/mle jd_signals -> rules can't break the title tie -> LLM is called
    def fake_llm(task, messages, **kw):
        return '{"type": "mle", "confidence": 0.7}'
    jd_b = Budget(remaining=5)
    d = route_one("AI Engineer / ML Engineer Intern", "generic body", CONFIG,
                  llm_complete=fake_llm, budget=jd_b, exhausted=True, title_budget=Budget(5))
    assert d.method == "llm" and jd_b.remaining == 4


def test_ambiguous_jd_zero_budget_defers_without_llm():
    """Ambiguous JD row with an exhausted budget defers and never calls the LLM."""
    b = Budget(remaining=0)
    d = route_one("AI Engineer / ML Engineer Intern", "generic body", CONFIG,
                  llm_complete=_llm_never, budget=b)
    assert d.resume_type is None and d.method is None
    assert b.remaining == 0


def test_exhausted_title_zero_title_budget_defers_without_llm():
    """An enrichment-exhausted no-signal title with a zero title budget defers, no LLM."""
    b = Budget(remaining=0)
    tb = Budget(remaining=0)
    d = route_one("Barista", None, CONFIG, llm_complete=_llm_never, budget=b,
                  exhausted=True, title_budget=tb)
    assert d.resume_type is None and d.method is None
    assert tb.remaining == 0


def test_clear_title_routes_with_all_budgets_zero():
    """Rules never need the LLM: a clear title still routes even with both budgets at 0."""
    b = Budget(remaining=0)
    tb = Budget(remaining=0)
    d = route_one("Software Engineer Intern", "api work", CONFIG, llm_complete=_llm_never,
                  budget=b, exhausted=True, title_budget=tb)
    assert d.resume_type == "swe" and d.method == "rules"


def test_no_signal_with_jd_calls_classify_open_and_spends_main_budget():
    # "Data Specialist Intern" / "work with data pipelines" matches no title_signal or jd_signal
    # in CONFIG (swe/ai/mle only) -> route_by_rules returns no_signal, WITH a real description.
    calls = []

    def fake_llm(task, messages, **kw):
        calls.append(1)
        return '{"type": "mle", "confidence": 0.83}'

    b = Budget(remaining=5)
    d = route_one("Data Specialist Intern", "work with data pipelines", CONFIG,
                  llm_complete=fake_llm, budget=b)
    assert d.method == "llm_open" and d.resume_type == "mle" and d.confidence == 0.83
    assert len(calls) == 1 and b.remaining == 4          # spent the MAIN budget, not title_budget


def test_no_signal_with_jd_none_answer_is_not_target():
    def fake_llm(task, messages, **kw):
        return '{"type": "none", "confidence": 0.9}'
    b = Budget(remaining=5)
    d = route_one("Warehouse Associate", "load and unload trucks", CONFIG,
                  llm_complete=fake_llm, budget=b)
    assert d.method == "not_target" and d.resume_type is None


def test_no_signal_with_jd_zero_budget_defers_without_llm():
    b = Budget(remaining=0)
    d = route_one("Data Specialist Intern", "work with data pipelines", CONFIG,
                  llm_complete=_llm_never, budget=b)
    assert d.resume_type is None and d.method is None
    assert b.remaining == 0
