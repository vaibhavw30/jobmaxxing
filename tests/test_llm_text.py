from jobmaxxing.llm.text import strip_code_fence


def test_strips_latex_fence():
    assert strip_code_fence("```latex\n\\documentclass{article}\n```") == r"\documentclass{article}"

def test_strips_plain_fence():
    assert strip_code_fence("```\nhello\n```") == "hello"

def test_no_fence_returned_unchanged():
    assert strip_code_fence(r"\documentclass{article}" + "\nBODY") == "\\documentclass{article}\nBODY"

def test_not_wholly_wrapped_is_unchanged():
    # a fence in the interior (not at the very start) must NOT be stripped
    tex = "\\begin{lstlisting}\n```\ncode\n```\n\\end{lstlisting}"
    assert strip_code_fence(tex) == tex
