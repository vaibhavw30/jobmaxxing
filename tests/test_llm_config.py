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
