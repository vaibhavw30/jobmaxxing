from jobmaxxing.routing.tiebreaker import build_tiebreaker_messages, parse_tiebreaker_response

CONFIG = {
    "types": {
        "ai": {"definition": "Applied generative AI."},
        "mle": {"definition": "ML engineering."},
        "swe": {"definition": "General software."},
    }
}


def test_build_messages_includes_candidates_and_definitions():
    msgs = build_tiebreaker_messages("AI/ML Intern", "build llm systems", ["ai", "mle"], CONFIG)
    assert msgs[0]["role"] == "system"
    blob = " ".join(m["content"] for m in msgs).lower()
    assert "ai" in blob and "mle" in blob
    assert "applied generative ai" in blob          # definition included
    assert "swe" not in blob                          # non-candidate type excluded
    assert "ai/ml intern" in blob and "build llm systems" in blob


def test_parse_valid_response():
    assert parse_tiebreaker_response('{"type": "ai", "confidence": 0.8}', ["ai", "mle"]) == ("ai", 0.8)


def test_parse_tolerates_code_fences_and_prose():
    text = 'Here is my answer:\n```json\n{"type":"mle","confidence":0.6}\n```'
    assert parse_tiebreaker_response(text, ["ai", "mle"]) == ("mle", 0.6)


def test_parse_rejects_out_of_candidate_type():
    assert parse_tiebreaker_response('{"type": "swe", "confidence": 0.9}', ["ai", "mle"]) is None


def test_parse_rejects_bad_confidence():
    assert parse_tiebreaker_response('{"type": "ai", "confidence": 5}', ["ai", "mle"]) is None
    assert parse_tiebreaker_response('{"type": "ai", "confidence": "high"}', ["ai", "mle"]) is None


def test_parse_rejects_garbage():
    assert parse_tiebreaker_response("not json at all", ["ai", "mle"]) is None
    assert parse_tiebreaker_response(None, ["ai", "mle"]) is None


def test_parse_rejects_two_objects_in_reply():
    text = '{"type": "ai", "confidence": 0.8} then {"type": "mle", "confidence": 0.5}'
    assert parse_tiebreaker_response(text, ["ai", "mle"]) is None


def test_parse_rejects_wrapped_object():
    assert parse_tiebreaker_response('{"result": {"type": "ai", "confidence": 0.8}}', ["ai", "mle"]) is None


def test_parse_accepts_boundary_confidence():
    assert parse_tiebreaker_response('{"type": "ai", "confidence": 0.0}', ["ai", "mle"]) == ("ai", 0.0)
    assert parse_tiebreaker_response('{"type": "ai", "confidence": 1.0}', ["ai", "mle"]) == ("ai", 1.0)
