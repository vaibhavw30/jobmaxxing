import json
import re

_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)
_EMPTY_CRITIQUE = {"weaknesses": [], "missing_keywords": []}

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
    return complete("tailor", messages, max_tokens=4000, cache=base_tex)


def parse_critique(text) -> dict:
    """Strict parse with a lenient fallback: any problem -> empty critique."""
    if not isinstance(text, str):
        return dict(_EMPTY_CRITIQUE)
    match = _JSON_OBJ.search(text)
    if not match:
        return dict(_EMPTY_CRITIQUE)
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return dict(_EMPTY_CRITIQUE)
    if not isinstance(data, dict):
        return dict(_EMPTY_CRITIQUE)
    weaknesses = data.get("weaknesses")
    missing = data.get("missing_keywords")
    if not isinstance(weaknesses, list) or not all(isinstance(w, str) for w in weaknesses):
        return dict(_EMPTY_CRITIQUE)
    if not isinstance(missing, list) or not all(isinstance(m, str) for m in missing):
        return dict(_EMPTY_CRITIQUE)
    return {"weaknesses": weaknesses[:3], "missing_keywords": missing}


def critique_resume(tailored_tex: str, jd: str, *, complete) -> dict:
    """Pass 2a: two-persona adversarial critique -> {weaknesses, missing_keywords}."""
    messages = [
        {"role": "system", "content": _REVIEW_SYSTEM},
        {"role": "user", "content": f"Job description:\n{jd}\n\nRésumé (LaTeX):\n{tailored_tex}"},
    ]
    text = complete("review", messages, max_tokens=1000, response_format={"type": "json_object"})
    return parse_critique(text)
