import json
import re

from ..llm.text import strip_code_fence

_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)
_DOC_START = r"\documentclass"
_DOC_END = r"\end{document}"


def _extract_latex(text: str) -> str:
    """Get compilable LaTeX out of a model response: strip a wrapping code fence, then — if a full
    document is present — return exactly the ``\\documentclass … \\end{document}`` span, discarding any
    prose the model added before (e.g. a '# Analysis' preamble) or after (trailing commentary). If no
    ``\\documentclass`` is present, return the fence-stripped text unchanged (a fragment/prose then fails
    compilation loudly rather than being silently mangled)."""
    text = strip_code_fence(text)
    start = text.find(_DOC_START)
    if start == -1:
        return text
    end = text.rfind(_DOC_END)
    if end == -1:
        return text[start:].strip()
    return text[start:end + len(_DOC_END)].strip()

_REVIEW_SYSTEM = (
    "You are two reviewers of a LaTeX résumé against a job description.\n"
    "- Senior engineer: name the 3 biggest weaknesses of the résumé for THIS role.\n"
    "- Hiring manager / ATS: list the important keywords/phrases the résumé is missing that a "
    "parser screening for this role would flag.\n"
    'Respond with STRICT JSON only: {"weaknesses": [3 strings], "missing_keywords": [strings]}.'
)

_TAILOR_SYSTEM = (
    "You tailor a LaTeX résumé to a specific job description.\n\n"
    "ABSOLUTE RULE — NO FABRICATION: never invent experience, skills, employers, dates, "
    "metrics, or claims. You may ONLY reorder, rephrase, and re-emphasize facts already "
    "present in the base résumé. If a desired keyword is not truthfully supported, leave it out.\n\n"
    "HARD CONSTRAINTS:\n"
    "- Surgical edits only.\n"
    "- Keep it to ONE page.\n"
    "- Preserve the template's structure, packages, and macros.\n"
    "Output ONLY the full LaTeX document, nothing else."
)


def build_tailored(base_tex: str, jd: str, *, complete) -> str:
    """Pass 1: produce the tailored .tex. The base résumé is prompt-cached."""
    messages = [
        {"role": "system", "content": _TAILOR_SYSTEM},
        {"role": "user", "content": f"Job description:\n{jd}\n\nProduce the full tailored LaTeX résumé."},
    ]
    return _extract_latex(complete("tailor", messages, max_tokens=4000, cache=base_tex))


def parse_critique(text) -> dict:
    """Strict parse with a lenient fallback: any problem -> empty critique."""
    if not isinstance(text, str):
        return {"weaknesses": [], "missing_keywords": []}  # fresh lists: never share the empty sentinel
    match = _JSON_OBJ.search(text)
    if not match:
        return {"weaknesses": [], "missing_keywords": []}  # fresh lists: never share the empty sentinel
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return {"weaknesses": [], "missing_keywords": []}  # fresh lists: never share the empty sentinel
    if not isinstance(data, dict):
        return {"weaknesses": [], "missing_keywords": []}  # fresh lists: never share the empty sentinel
    weaknesses = data.get("weaknesses")
    missing = data.get("missing_keywords")
    if not isinstance(weaknesses, list) or not all(isinstance(w, str) for w in weaknesses):
        return {"weaknesses": [], "missing_keywords": []}  # fresh lists: never share the empty sentinel
    if not isinstance(missing, list) or not all(isinstance(m, str) for m in missing):
        return {"weaknesses": [], "missing_keywords": []}  # fresh lists: never share the empty sentinel
    return {"weaknesses": weaknesses[:3], "missing_keywords": missing}


def critique_resume(tailored_tex: str, jd: str, *, complete) -> dict:
    """Pass 2a: two-persona adversarial critique -> {weaknesses, missing_keywords}."""
    messages = [
        {"role": "system", "content": _REVIEW_SYSTEM},
        {"role": "user", "content": f"Job description:\n{jd}\n\nRésumé (LaTeX):\n{tailored_tex}"},
    ]
    # response_format is honored by OpenAI/xAI; the Anthropic adapter ignores it, so the
    # prompt demands STRICT JSON and parse_critique is the real (lenient) schema gate.
    text = complete("review", messages, max_tokens=1000, response_format={"type": "json_object"})
    return parse_critique(text)


_PATCH_SYSTEM = (
    "Revise the LaTeX résumé to address the reviewer feedback. Same hard constraints: "
    "surgical edits, NO fabrication, ONE page, preserve template. Output ONLY the full LaTeX document."
)
_SHRINK_SYSTEM = (
    "The compiled résumé overflowed one page. Cut it to EXACTLY one page with surgical removals "
    "(trim the least-relevant content), NO fabrication, preserve template. Output ONLY the full LaTeX document."
)


def apply_critique(tailored_tex: str, critique: dict, jd: str, *, complete) -> str:
    """Pass 2b: apply the critique's fixes -> patched .tex."""
    weaknesses = "\n".join(f"- {w}" for w in critique.get("weaknesses", []))
    missing = ", ".join(critique.get("missing_keywords", []))
    messages = [
        {"role": "system", "content": _PATCH_SYSTEM},
        {"role": "user", "content": (
            f"Job description:\n{jd}\n\nReviewer weaknesses:\n{weaknesses}\n\n"
            f"Missing keywords to incorporate where truthful: {missing}\n\n"
            f"Current résumé (LaTeX):\n{tailored_tex}"
        )},
    ]
    return _extract_latex(complete("review", messages, max_tokens=4000))


def shrink_to_one_page(tex: str, page_count: int, *, complete) -> str:
    """The shrink_fn used by the one-page guard."""
    messages = [
        {"role": "system", "content": _SHRINK_SYSTEM},
        {"role": "user", "content": f"The résumé compiled to {page_count} pages. Cut it to one page.\n\n{tex}"},
    ]
    return _extract_latex(complete("tailor", messages, max_tokens=4000))
