from jobmaxxing.tailoring.scorer import parse_qualitative, score_qualitative

AXES = {"technical_depth", "impact", "ats", "relevance_order"}


def test_parse_good_json():
    out = parse_qualitative('{"technical_depth": 7, "impact": 5, "ats": 9, "relevance_order": 6}')
    assert out == {"technical_depth": 7.0, "impact": 5.0, "ats": 9.0, "relevance_order": 6.0}

def test_parse_clamps_out_of_range():
    out = parse_qualitative('{"technical_depth": 15, "impact": -3, "ats": 9, "relevance_order": 6}')
    assert out["technical_depth"] == 10.0 and out["impact"] == 0.0

def test_parse_malformed_is_neutral():
    for bad in ["not json", '{"technical_depth": 7}', '{"technical_depth":"x","impact":1,"ats":1,"relevance_order":1}', "", None]:
        assert parse_qualitative(bad) == {a: 5.0 for a in AXES}

def test_score_qualitative_calls_score_tier_at_temp0():
    captured = {}
    def fake_complete(task, messages, *, max_tokens, temperature=None, **kw):
        captured["task"] = task
        captured["temperature"] = temperature
        return '{"technical_depth": 8, "impact": 8, "ats": 8, "relevance_order": 8}'
    out = score_qualitative("RESUME", "JD", {"keyword_dict": ["python", "kubernetes"]}, complete=fake_complete)
    assert out["ats"] == 8.0
    assert captured["task"] == "score" and captured["temperature"] == 0
