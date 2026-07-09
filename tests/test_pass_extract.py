from jobmaxxing.tailoring.passes import _extract_latex

DOC = "\\documentclass{article}\n\\begin{document}\nHi\n\\end{document}"

def test_strips_leading_prose():
    assert _extract_latex("# Analysis\nHere is the resume:\n\n" + DOC) == DOC

def test_strips_trailing_prose():
    assert _extract_latex(DOC + "\n\nHope this helps! Let me know.") == DOC

def test_strips_fence_then_extracts():
    assert _extract_latex("```latex\n" + DOC + "\n```") == DOC

def test_clean_doc_unchanged():
    assert _extract_latex(DOC) == DOC

def test_no_documentclass_returned_as_is():
    assert _extract_latex("just some prose, no latex here") == "just some prose, no latex here"
