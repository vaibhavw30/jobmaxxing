"""Structural asserts on the routing workflows (no network). Guards the every-4-days LLM split."""

import pathlib

import yaml

WF = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _load(name):
    return yaml.safe_load((WF / name).read_text())


def _on(wf):
    # PyYAML (YAML 1.1) parses a bare `on:` key as boolean True, not the string "on".
    return wf.get("on") or wf.get(True)


def _steps(wf):
    (job,) = wf["jobs"].values()
    return job["steps"]


def _route_step(wf):
    for s in _steps(wf):
        if "run" in s and "jobmaxxing.route" in s["run"]:
            return s
    raise AssertionError("no route step found")


def test_pollers_routes_rules_only_and_has_no_api_keys():
    wf = _load("pollers.yml")
    step = _route_step(wf)
    assert "--no-llm" in step["run"]
    env = step.get("env", {})
    assert not any(k.endswith("_API_KEY") for k in env), f"pollers route step still has API keys: {env}"


def test_llm_route_workflow_runs_full_llm_every_4_days():
    wf = _load("llm-route.yml")
    crons = [c["cron"] for c in _on(wf)["schedule"]]
    assert "0 6 */4 * *" in crons
    step = _route_step(wf)
    assert "--no-llm" not in step["run"]
    env = step.get("env", {})
    assert {"OPENAI_API_KEY", "XAI_API_KEY", "ANTHROPIC_API_KEY"} <= set(env)
    runs = [s.get("run", "") for s in _steps(wf)]
    migrate_i = next(i for i, r in enumerate(runs) if "jobmaxxing.migrate" in r)
    route_i = next(i for i, r in enumerate(runs) if "jobmaxxing.route" in r)
    assert migrate_i < route_i
