import importlib


def test_phase2_packages_import():
    assert importlib.import_module("jobmaxxing.llm")
    assert importlib.import_module("jobmaxxing.routing")


def test_llm_sdks_available():
    assert importlib.import_module("openai")
    assert importlib.import_module("anthropic")
