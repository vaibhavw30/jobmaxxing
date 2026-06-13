import importlib


def test_mcp_package_imports():
    assert importlib.import_module("jobmaxxing.mcp")


def test_mcp_sdk_available():
    assert importlib.import_module("mcp.server.fastmcp")
