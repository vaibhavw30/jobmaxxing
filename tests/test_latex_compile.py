import shutil

import pytest

from jobmaxxing.tailoring.latex import CompileResult, LatexError, compile_pdf

_HAS_PDFLATEX = shutil.which("pdflatex") is not None
_ONE_PAGE_TEX = r"""
\documentclass{article}
\begin{document}
Hello one page.
\end{document}
"""


@pytest.mark.skipif(not _HAS_PDFLATEX, reason="pdflatex not installed")
def test_compile_pdf_returns_one_page():
    result = compile_pdf(_ONE_PAGE_TEX)
    assert isinstance(result, CompileResult)
    assert result.page_count == 1
    assert result.pdf_bytes.startswith(b"%PDF")


@pytest.mark.skipif(not _HAS_PDFLATEX, reason="pdflatex not installed")
def test_compile_pdf_raises_on_invalid_tex():
    with pytest.raises(LatexError):
        compile_pdf(r"\documentclass{article}\begin{document}\undefinedcmd")
