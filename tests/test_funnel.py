from jobmaxxing.funnel import VALID_STATUSES, decision_to_status, plain_text


def test_decision_applied_wins():
    assert decision_to_status("Yes", "TRUE", "routed") == "applied"
    assert decision_to_status("", "TRUE", "applied") is None


def test_decision_yes_only_from_new_or_routed():
    assert decision_to_status("Yes", "", "routed") == "approved_for_tailoring"
    assert decision_to_status("Yes", "", "tailored") is None


def test_decision_no_rejects_and_blank_noops():
    assert decision_to_status("not interested", "", "tailored") == "rejected"
    assert decision_to_status("No", "", "rejected") is None
    assert decision_to_status("maybe", "FALSE", "routed") is None


def test_plain_text_strips_collapses_unescapes_truncates():
    assert plain_text("<p>Hello   <b>world</b></p>") == "Hello world"
    assert plain_text(None) == ""
    assert plain_text("Tom &amp; Jerry") == "Tom & Jerry"
    assert len(plain_text("x" * 50000, limit=100)) == 100


def test_valid_statuses_complete():
    assert VALID_STATUSES == {
        "new", "routed", "approved_for_tailoring", "tailored", "reviewed", "applied", "rejected",
    }
