from jobmaxxing.routing.rules import route_by_rules

CONFIG = {
    "weights": {"title": 3.0, "jd": 1.0},
    "thresholds": {"min_top_score": 1.0, "min_margin_ratio": 0.5, "jd_hits_cap": 5},
    "types": {
        "swe": {"title_signals": ["software engineer"], "jd_signals": ["api", "rest"], "exclude_signals": []},
        "mle": {"title_signals": ["ml engineer"], "jd_signals": ["training", "pytorch"], "exclude_signals": []},
        "ai": {"title_signals": ["ai engineer"], "jd_signals": ["llm", "rag"], "exclude_signals": []},
    },
}


def test_single_title_match_routes_deterministically():
    o = route_by_rules("Software Engineer Intern", "irrelevant body", CONFIG)
    assert o.decision == "routed" and o.resume_type == "swe"
    assert o.confidence >= 0.7


def test_single_title_match_wins_over_conflicting_jd():
    # title says SWE; body is full of ML keywords -> title is authoritative, no ambiguity
    o = route_by_rules("Software Engineer Intern", "training pytorch llm rag", CONFIG)
    assert o.decision == "routed" and o.resume_type == "swe"


def test_two_title_matches_broken_by_jd_margin():
    o = route_by_rules("AI Engineer / ML Engineer Intern", "we build llm and rag systems", CONFIG)
    assert o.decision == "routed" and o.resume_type == "ai"   # jd favors ai clearly


def test_two_title_matches_no_jd_separation_is_ambiguous():
    o = route_by_rules("AI Engineer / ML Engineer Intern", "generic team description", CONFIG)
    assert o.decision == "ambiguous"
    assert set(o.candidates) == {"ai", "mle"}


def test_no_title_signal_routes_from_jd_when_clear():
    o = route_by_rules("Summer Intern", "build rest api endpoints", CONFIG)
    assert o.decision == "routed" and o.resume_type == "swe"


def test_no_title_no_jd_is_no_signal():
    o = route_by_rules("Summer Intern", None, CONFIG)
    assert o.decision == "no_signal"


def test_no_title_ambiguous_jd_is_ambiguous():
    # jd has one hit for swe and one for ai -> tie, no clear margin
    o = route_by_rules("Summer Intern", "rest api with llm", CONFIG)
    assert o.decision == "ambiguous"
    assert set(o.candidates) == {"swe", "ai"}
