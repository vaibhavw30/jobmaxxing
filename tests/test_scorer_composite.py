from jobmaxxing.tailoring.scorer import score, delta

RUBRIC = {
    "keyword_dict": ["python", "kubernetes"], "aliases": {},
    "weights": {"keyword_coverage": 0.2, "technical_depth": 0.2, "impact": 0.2, "ats": 0.2, "relevance_order": 0.2},
}


def _fake_complete(task, messages, *, max_tokens, temperature=None, **kw):
    return '{"technical_depth": 6, "impact": 4, "ats": 8, "relevance_order": 7}'


def test_score_is_additive_superset_with_axes_and_composite():
    out = score("python kubernetes", "python kubernetes role", RUBRIC, complete=_fake_complete)
    assert {"static", "dynamic", "matched", "missing"} <= set(out)     # backward-compatible keys kept
    assert out["axes"]["keyword_coverage"] == 10.0                     # 10 x dynamic (both terms in JD+resume)
    assert out["axes"] == {"keyword_coverage": 10.0, "technical_depth": 6.0, "impact": 4.0, "ats": 8.0, "relevance_order": 7.0}
    # equal 0.2 weights: 0.2*(10+6+4+8+7) = 7.0
    assert out["composite"] == 7.0

def test_delta_reports_composite_and_axes():
    before = score("python", "python kubernetes role", RUBRIC, complete=lambda *a, **k: '{"technical_depth":2,"impact":2,"ats":2,"relevance_order":2}')
    after = score("python kubernetes", "python kubernetes role", RUBRIC, complete=_fake_complete)
    d = delta(before, after)
    assert "composite" in d and "axes" in d and "static" in d and "dynamic" in d
    assert d["axes"]["ats"] == 6.0                                     # 8 - 2
