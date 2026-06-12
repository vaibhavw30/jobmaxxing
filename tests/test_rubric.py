import pytest

from jobmaxxing.routing.types import VALID_TYPES
from jobmaxxing.tailoring.rubric import RubricMissing, load_rubric


def test_load_rubric_returns_keyword_dict_and_aliases():
    r = load_rubric("swe")
    assert isinstance(r["keyword_dict"], list) and r["keyword_dict"]
    assert isinstance(r["aliases"], dict)


def test_every_type_has_a_rubric():
    for t in VALID_TYPES:
        r = load_rubric(t)
        assert r["keyword_dict"], f"{t} has empty keyword_dict"


def test_load_rubric_missing_type_raises(tmp_path):
    with pytest.raises(RubricMissing):
        load_rubric("nonexistent", base_dir=tmp_path)
