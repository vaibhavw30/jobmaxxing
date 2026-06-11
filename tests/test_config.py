from jobmaxxing.config import load_watchlist


def test_load_watchlist_missing_file_returns_empty(tmp_path):
    assert load_watchlist(tmp_path / "nope.yaml") == []


def test_load_watchlist_null_companies_returns_empty(tmp_path):
    p = tmp_path / "wl.yaml"
    p.write_text("companies:\n")
    assert load_watchlist(p) == []


def test_load_watchlist_non_list_returns_empty(tmp_path):
    p = tmp_path / "wl.yaml"
    p.write_text("companies: not-a-list\n")
    assert load_watchlist(p) == []


def test_load_watchlist_valid_entries(tmp_path):
    p = tmp_path / "wl.yaml"
    p.write_text("companies:\n  - company: Acme\n    ats: greenhouse\n    token: acme\n")
    assert load_watchlist(p) == [{"company": "Acme", "ats": "greenhouse", "token": "acme"}]
