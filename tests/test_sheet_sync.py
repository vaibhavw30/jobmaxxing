from jobmaxxing.sheets.sync import DATA_COLS, DECISION_COLS, HEADER, _intended_status, _plain


def test_header_is_data_then_decision():
    assert HEADER == DATA_COLS + DECISION_COLS
    assert DATA_COLS[0] == "job_id" and DECISION_COLS == ["interested", "applied"]


def test_plain_strips_html_collapses_and_truncates():
    assert _plain("<p>Hello   <b>world</b></p>") == "Hello world"
    assert _plain(None) == ""
    assert len(_plain("x" * 50000, limit=100)) == 100


def test_intended_status_applied_wins():
    assert _intended_status("Yes", "TRUE", "routed") == "applied"
    assert _intended_status("", "true", "new") == "applied"
    assert _intended_status("", "TRUE", "applied") is None       # already applied -> no-op


def test_intended_status_yes_only_from_new_or_routed():
    assert _intended_status("Yes", "", "routed") == "approved_for_tailoring"
    assert _intended_status("interested", "", "new") == "approved_for_tailoring"
    assert _intended_status("Yes", "", "tailored") is None       # no regress
    assert _intended_status("Yes", "", "reviewed") is None


def test_intended_status_no_rejects_and_blank_noops():
    assert _intended_status("No", "", "routed") == "rejected"
    assert _intended_status("not interested", "", "tailored") == "rejected"
    assert _intended_status("No", "", "rejected") is None        # already rejected
    assert _intended_status("", "", "routed") is None
    assert _intended_status("maybe", "FALSE", "routed") is None
