import importlib


def test_phase2_packages_import():
    assert importlib.import_module("jobmaxxing.llm")
    assert importlib.import_module("jobmaxxing.routing")


def test_llm_sdks_available():
    assert importlib.import_module("openai")
    assert importlib.import_module("anthropic")


def test_route_entrypoint_module_resolves():
    # `python -m jobmaxxing.route` is the documented CLI + the CI route step; the top-level
    # shim must exist and expose main (the impl lives in jobmaxxing.routing.route).
    import jobmaxxing.route as entry
    from jobmaxxing.routing.route import main as impl_main
    assert entry.main is impl_main
