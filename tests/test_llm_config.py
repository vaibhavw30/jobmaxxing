from jobmaxxing.llm.config import candidates_for, load_llm_config


def test_load_llm_config_reads_tasks(tmp_path):
    p = tmp_path / "llm.yaml"
    p.write_text(
        "tasks:\n"
        "  route:\n"
        "    - {provider: openai, model: gpt-4o-mini}\n"
        "    - {provider: xai, model: grok-3-mini}\n"
    )
    cfg = load_llm_config(p)
    assert candidates_for("route", cfg) == [
        {"provider": "openai", "model": "gpt-4o-mini"},
        {"provider": "xai", "model": "grok-3-mini"},
    ]


def test_candidates_for_unknown_task_is_empty(tmp_path):
    p = tmp_path / "llm.yaml"
    p.write_text("tasks: {}\n")
    assert candidates_for("route", load_llm_config(p)) == []


def test_load_llm_config_missing_file_returns_empty(tmp_path):
    cfg = load_llm_config(tmp_path / "nope.yaml")
    assert candidates_for("route", cfg) == []


def test_candidates_for_handles_malformed_config():
    assert candidates_for("route", {}) == []
    assert candidates_for("route", {"tasks": "nope"}) == []
    assert candidates_for("route", {"tasks": {"route": "nope"}}) == []
    assert candidates_for("route", "not-a-dict") == []


def test_candidates_for_filters_incomplete_candidates():
    cfg = {"tasks": {"route": [
        {"provider": "openai", "model": "gpt-4o-mini"},
        {"provider": "openai"},   # missing model -> dropped
        {"model": "x"},           # missing provider -> dropped
        "garbage",                # not a dict -> dropped
    ]}}
    assert candidates_for("route", cfg) == [{"provider": "openai", "model": "gpt-4o-mini"}]


def test_tailor_and_review_prefer_claude_cli():
    from jobmaxxing.llm.config import candidates_for, load_llm_config
    cfg = load_llm_config()  # the real config/llm.yaml
    for task in ("tailor", "review"):
        cands = candidates_for(task, cfg)
        assert cands[0] == {"provider": "claude-cli", "model": "sonnet"}, task
        assert any(c["provider"] == "anthropic" for c in cands[1:]), f"{task} keeps an API fallback"
    # route must NOT use claude-cli (CI has no subscription; stays API)
    assert all(c["provider"] != "claude-cli" for c in candidates_for("route", cfg))


def test_score_tier_prefers_anthropic_api():
    from jobmaxxing.llm.config import candidates_for, load_llm_config
    cands = candidates_for("score", load_llm_config())
    assert cands[0]["provider"] == "anthropic"          # API first -> temperature honored
    assert any(c["provider"] == "claude-cli" for c in cands)  # subscription fallback present
