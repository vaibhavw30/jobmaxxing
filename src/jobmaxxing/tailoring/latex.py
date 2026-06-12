import io
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader


class LatexError(RuntimeError):
    """Raised when pdflatex fails to produce a PDF."""


@dataclass
class CompileResult:
    pdf_bytes: bytes
    page_count: int
    log: str


def compile_pdf(tex: str, *, runs: int = 2) -> CompileResult:
    """Compile LaTeX to PDF with pdflatex; measure page count from the PDF via pypdf.

    Runs pdflatex `runs` times (refs/labels settle on the 2nd pass). Raises LatexError
    with the log tail if no PDF is produced.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tex_file = tmp_path / "resume.tex"
        tex_file.write_text(tex)
        log = ""
        for _ in range(runs):
            proc = subprocess.run(
                ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "resume.tex"],
                cwd=tmp_path,
                capture_output=True,
                text=True,
            )
            log = proc.stdout + proc.stderr
        pdf_file = tmp_path / "resume.pdf"
        if not pdf_file.exists():
            raise LatexError(f"pdflatex produced no PDF. Log tail:\n{log[-2000:]}")
        pdf_bytes = pdf_file.read_bytes()
    page_count = len(PdfReader(io.BytesIO(pdf_bytes)).pages)
    return CompileResult(pdf_bytes=pdf_bytes, page_count=page_count, log=log)
