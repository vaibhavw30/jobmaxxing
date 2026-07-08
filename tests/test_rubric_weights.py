import json
from pathlib import Path

import pytest

from jobmaxxing.config import REPO_ROOT
from jobmaxxing.tailoring.rubric import load_rubric

AXES = {"keyword_coverage", "technical_depth", "impact", "ats", "relevance_order"}
RUBRICS = sorted((REPO_ROOT / "rubrics").glob("*.json"))


@pytest.mark.parametrize("path", RUBRICS, ids=lambda p: p.stem)
def test_weights_present_and_sum_to_one(path):
    w = json.loads(path.read_text())["weights"]
    assert set(w) == AXES
    assert abs(sum(w.values()) - 1.0) < 1e-9

def test_load_rubric_defaults_weights(tmp_path):
    (tmp_path / "swe.json").write_text('{"keyword_dict": ["python"]}')
    w = load_rubric("swe", base_dir=tmp_path)["weights"]
    assert set(w) == AXES and abs(sum(w.values()) - 1.0) < 1e-9
