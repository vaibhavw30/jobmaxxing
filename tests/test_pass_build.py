from jobmaxxing.tailoring.passes import build_tailored


def test_build_tailored_passes_cache_and_jd():
    captured = {}

    def fake_complete(task, messages, *, max_tokens, cache=None, **kw):
        captured["task"] = task
        captured["cache"] = cache
        captured["user"] = next(m["content"] for m in messages if m["role"] == "user")
        return r"\documentclass{article}...TAILORED"

    out = build_tailored("BASE TEX", "Senior SWE, needs Kubernetes.", complete=fake_complete)
    assert out == r"\documentclass{article}...TAILORED"
    assert captured["task"] == "tailor"
    assert captured["cache"] == "BASE TEX"             # base resume prompt-cached
    assert "Kubernetes" in captured["user"]            # JD passed in the user message
