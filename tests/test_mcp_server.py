def test_server_module_imports_without_connecting():
    import jobmaxxing.mcp.server as server

    assert server.mcp is not None
    assert callable(server.main)


def test_all_seven_tools_registered():
    import jobmaxxing.mcp.server as server

    for name in ("query_jobs", "preview_route", "set_route", "approve",
                 "tailor_job", "get_review", "set_status"):
        assert callable(getattr(server, name)), f"missing tool wrapper: {name}"


def test_entrypoint_module_resolves():
    import importlib

    assert importlib.import_module("jobmaxxing.mcp.__main__")
