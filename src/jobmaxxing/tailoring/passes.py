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
