from pathlib import Path

import yaml

from ..config import REPO_ROOT


def load_routing_config(path: Path | None = None) -> dict:
    """Load config/routing.yaml. Missing file -> empty dict."""
    path = path or REPO_ROOT / "config" / "routing.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}
