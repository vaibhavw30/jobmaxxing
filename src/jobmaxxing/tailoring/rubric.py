import json
from pathlib import Path

from ..config import REPO_ROOT


class RubricMissing(RuntimeError):
    """Raised when no rubric file exists for a resume type."""


def load_rubric(resume_type: str, base_dir: Path | None = None) -> dict:
    """Load rubrics/{resume_type}.json -> {keyword_dict, aliases, weights}."""
    base_dir = base_dir or REPO_ROOT / "rubrics"
    path = base_dir / f"{resume_type}.json"
    if not path.exists():
        raise RubricMissing(f"no rubric for resume_type {resume_type!r} at {path}")
    data = json.loads(path.read_text())
    data.setdefault("keyword_dict", [])
    data.setdefault("aliases", {})
    data.setdefault("weights", {"keyword_coverage": 0.2, "technical_depth": 0.2,
                                "impact": 0.2, "ats": 0.2, "relevance_order": 0.2})
    return data
