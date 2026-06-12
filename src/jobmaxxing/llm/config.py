from pathlib import Path

import yaml

from ..config import REPO_ROOT


def load_llm_config(path: Path | None = None) -> dict:
    """Load config/llm.yaml. Missing file -> empty config (no candidates)."""
    path = path or REPO_ROOT / "config" / "llm.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def candidates_for(task: str, config: dict) -> list[dict]:
    """Ordered (provider, model) candidate dicts for a task; [] if none/malformed."""
    tasks = config.get("tasks") if isinstance(config, dict) else None
    if not isinstance(tasks, dict):
        return []
    candidates = tasks.get(task)
    if not isinstance(candidates, list):
        return []
    return [c for c in candidates if isinstance(c, dict) and c.get("provider") and c.get("model")]
