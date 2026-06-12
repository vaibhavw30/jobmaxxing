from jobmaxxing.routing.config import load_routing_config
from jobmaxxing.routing.types import VALID_TYPES, Budget, RouteDecision, RulesOutcome


def test_valid_types_are_the_eight():
    assert VALID_TYPES == (
        "quant-trader", "quant-dev", "mle", "swe", "fdse", "ai", "robotics", "av",
    )


def test_dataclass_defaults():
    o = RulesOutcome(decision="no_signal")
    assert o.resume_type is None and o.confidence == 0.0 and o.candidates == []
    d = RouteDecision(resume_type=None, method=None)
    assert d.confidence == 0.0
    b = Budget(remaining=3)
    assert b.remaining == 3


def test_routing_config_has_all_types_with_signals():
    cfg = load_routing_config()
    assert set(cfg["types"]) == set(VALID_TYPES)
    for t in VALID_TYPES:
        spec = cfg["types"][t]
        assert spec["definition"]
        assert isinstance(spec["title_signals"], list) and spec["title_signals"]
        assert isinstance(spec["jd_signals"], list)
    assert cfg["weights"]["title"] >= cfg["weights"]["jd"]
    assert "min_margin_ratio" in cfg["thresholds"]


def test_load_routing_config_missing_file_returns_empty(tmp_path):
    assert load_routing_config(tmp_path / "nope.yaml") == {}
