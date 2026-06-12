import io
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError


class LatexError(RuntimeError):
    """The single failure type for compilation: missing binary, timeout, no PDF, or unreadable PDF."""


@dataclass
class CompileResult:
    pdf_bytes: bytes
    page_count: int
    log: str


def compile_pdf(tex: str, *, runs: int = 2, timeout: float = 60.0) -> CompileResult:
    """Compile LaTeX to PDF with pdflatex; measure page count from the PDF via pypdf.

    Runs pdflatex `runs` times (refs/labels settle on the 2nd pass). Every failure mode
    surfaces as LatexError so callers catch one exception type: a missing `pdflatex`
    binary, a hang (>`timeout`s), no PDF produced, or an unreadable PDF.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "resume.tex").write_text(tex)
        log_parts: list[str] = []
        for i in range(runs):
            try:
                proc = subprocess.run(
                    ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "resume.tex"],
                    cwd=tmp_path,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                )
            except FileNotFoundError as exc:
                raise LatexError("pdflatex not found on PATH; install a LaTeX distribution") from exc
            except subprocess.TimeoutExpired as exc:
                raise LatexError(f"pdflatex timed out after {timeout}s") from exc
            log_parts.append(f"--- run {i + 1} ---\n{proc.stdout}{proc.stderr}")
        log = "\n".join(log_parts)
        pdf_file = tmp_path / "resume.pdf"
        if not pdf_file.exists():
            raise LatexError(f"pdflatex produced no PDF. Log tail:\n{log[-2000:]}")
        pdf_bytes = pdf_file.read_bytes()
    try:
        page_count = len(PdfReader(io.BytesIO(pdf_bytes)).pages)
    except PdfReadError as exc:
        raise LatexError(f"pdflatex produced an unreadable PDF: {exc}") from exc
    return CompileResult(pdf_bytes=pdf_bytes, page_count=page_count, log=log)


@dataclass
class OnePageResult:
    tex: str
    pdf_bytes: bytes
    page_count: int
    retries: int
    fit: bool


def enforce_one_page(
    tex: str,
    *,
    compile_fn: Callable[[str], CompileResult],
    shrink_fn: Callable[[str, int], str],
    max_retries: int = 3,
) -> OnePageResult:
    """Compile; if it overflows one page, ask shrink_fn to cut and recompile, up to
    max_retries shrink+recompile cycles. The page count is always measured by compile_fn,
    never self-reported. If it never fits, return the last attempt flagged fit=False."""
    result = compile_fn(tex)
    if result.page_count <= 1:
        return OnePageResult(tex, result.pdf_bytes, result.page_count, 0, True)
    for attempt in range(1, max_retries + 1):
        tex = shrink_fn(tex, result.page_count)
        result = compile_fn(tex)
        if result.page_count <= 1:
            return OnePageResult(tex, result.pdf_bytes, result.page_count, attempt, True)
    return OnePageResult(tex, result.pdf_bytes, result.page_count, max_retries, False)
