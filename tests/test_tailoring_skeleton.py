import importlib


def test_tailoring_package_imports():
    assert importlib.import_module("jobmaxxing.tailoring")


def test_tailoring_deps_available():
    assert importlib.import_module("boto3")
    assert importlib.import_module("pypdf")
