from jobmaxxing.tailoring.diffing import unified_diff


def test_unified_diff_shows_changed_lines():
    out = unified_diff("line one\nline two\n", "line one\nline TWO\n")
    assert "-line two" in out
    assert "+line TWO" in out
    assert "base.tex" in out and "tailored.tex" in out


def test_unified_diff_identical_is_empty():
    assert unified_diff("same\n", "same\n") == ""
