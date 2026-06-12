from jobmaxxing.tailoring.passes import critique_resume, parse_critique


def test_parse_valid_critique():
    out = parse_critique('{"weaknesses": ["a", "b", "c"], "missing_keywords": ["kafka"]}')
    assert out == {"weaknesses": ["a", "b", "c"], "missing_keywords": ["kafka"]}


def test_parse_caps_weaknesses_at_three_and_tolerates_prose():
    out = parse_critique('here:\n```json\n{"weaknesses":["a","b","c","d"],"missing_keywords":[]}\n```')
    assert out["weaknesses"] == ["a", "b", "c"]


def test_parse_garbage_yields_empty_critique():
    assert parse_critique("not json") == {"weaknesses": [], "missing_keywords": []}
    assert parse_critique('{"weaknesses": "nope"}') == {"weaknesses": [], "missing_keywords": []}


def test_critique_resume_calls_review_task():
    captured = {}

    def fake_complete(task, messages, *, max_tokens, response_format=None, **kw):
        captured["task"] = task
        return '{"weaknesses": ["w1", "w2", "w3"], "missing_keywords": ["rust"]}'

    out = critique_resume("TAILORED TEX", "JD text", complete=fake_complete)
    assert captured["task"] == "review"
    assert out["missing_keywords"] == ["rust"]


def test_empty_critique_lists_are_not_shared():
    # a mutating caller must not poison subsequent empty critiques
    r1 = parse_critique("garbage")
    r1["weaknesses"].append("mutated")
    r2 = parse_critique("garbage")
    assert r2["weaknesses"] == []


def test_critique_resume_requests_json_object():
    captured = {}

    def fake_complete(task, messages, *, max_tokens, response_format=None, **kw):
        captured["response_format"] = response_format
        return '{"weaknesses": [], "missing_keywords": []}'

    critique_resume("TEX", "JD", complete=fake_complete)
    assert captured["response_format"] == {"type": "json_object"}
