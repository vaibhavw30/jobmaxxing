from jobmaxxing.tailoring.passes import apply_critique, shrink_to_one_page


def test_apply_critique_returns_patched_tex():
    captured = {}

    def fake_complete(task, messages, *, max_tokens, **kw):
        captured["task"] = task
        captured["user"] = next(m["content"] for m in messages if m["role"] == "user")
        return r"\documentclass...PATCHED"

    critique = {"weaknesses": ["thin on scale"], "missing_keywords": ["kafka"]}
    out = apply_critique("TAILORED", critique, "JD", complete=fake_complete)
    assert out == r"\documentclass...PATCHED"
    assert captured["task"] == "review"
    assert "kafka" in captured["user"]              # critique fed back into the patch prompt


def test_shrink_to_one_page_mentions_page_count():
    captured = {}

    def fake_complete(task, messages, *, max_tokens, **kw):
        captured["user"] = next(m["content"] for m in messages if m["role"] == "user")
        return r"\documentclass...SHORTER"

    out = shrink_to_one_page("TOO LONG TEX", 2, complete=fake_complete)
    assert out == r"\documentclass...SHORTER"
    assert "2" in captured["user"]                  # tells the model how many pages it overflowed to
