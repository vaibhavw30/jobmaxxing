from jobmaxxing.routing.rules import _count_hits, _norm, score_jd, score_title

CONFIG = {
    "weights": {"title": 3.0, "jd": 1.0},
    "thresholds": {"jd_hits_cap": 5},
    "types": {
        "swe": {"title_signals": ["software engineer"], "jd_signals": ["api", "rest"], "exclude_signals": []},
        "ai": {"title_signals": ["ai engineer"], "jd_signals": ["llm", "rag"], "exclude_signals": []},
        "mle": {"title_signals": ["ml engineer"], "jd_signals": ["training"], "exclude_signals": ["llm"]},
    },
}


def test_norm_lowercases_and_collapses_whitespace_keeps_punct():
    assert _norm("Low-Latency   C++") == "low-latency c++"


def test_count_hits_is_boundary_aware():
    assert _count_hits("training the model", ["ai"]) == 0      # 'ai' inside 'training' must NOT match
    assert _count_hits("an ai system", ["ai"]) == 1
    assert _count_hits("we use c++ daily", ["c++"]) == 1       # punctuated token matches
    assert _count_hits("c is fine", ["c++"]) == 0


def test_count_hits_counts_unique_signals():
    assert _count_hits("api and rest api", ["api", "rest"]) == 2


def test_score_title():
    scores = score_title("Software Engineer Intern", CONFIG)
    assert scores["swe"] == 1 and scores["ai"] == 0 and scores["mle"] == 0


def test_score_jd_caps_and_applies_exclusions():
    cfg = {
        "thresholds": {"jd_hits_cap": 1},
        "types": {"swe": {"jd_signals": ["api", "rest"], "exclude_signals": []}},
    }
    assert score_jd("api rest", cfg)["swe"] == 1.0            # capped at 1

    scores = score_jd("we ship llm training pipelines", CONFIG)
    assert scores["mle"] == 0.0                                # 1 hit (training) - 1 exclusion (llm)


def test_score_jd_no_description_is_zero():
    assert score_jd(None, CONFIG) == {"swe": 0.0, "ai": 0.0, "mle": 0.0}


def test_count_hits_matches_multi_word_signal_across_collapsed_whitespace():
    # multi-word signal must match even when the text had irregular spacing
    assert _count_hits(_norm("we do  market   making here"), ["market making"]) == 1
    assert _count_hits(_norm("low-latency trading"), ["low-latency"]) == 1


def test_count_hits_dedupes_repeated_config_signals():
    assert _count_hits("llm llm llm", ["llm", "llm"]) == 1   # repeat in config counts once


def test_score_jd_can_go_negative_when_exclusions_dominate():
    cfg = {"thresholds": {"jd_hits_cap": 5},
           "types": {"mle": {"jd_signals": ["training"], "exclude_signals": ["llm", "agentic"]}}}
    assert score_jd("llm agentic training", cfg)["mle"] == -1.0   # 1 hit - 2 exclusions
