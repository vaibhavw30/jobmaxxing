import re

_WS = re.compile(r"\s+")
_pattern_cache: dict[str, re.Pattern] = {}


def _norm(text: str) -> str:
    """Lowercase, collapse whitespace; keep punctuation (tech terms like c++ need it)."""
    return _WS.sub(" ", (text or "").lower()).strip()


def _pattern(token: str) -> re.Pattern:
    pat = _pattern_cache.get(token)
    if pat is None:
        pat = re.compile(r"(?<![a-z0-9])" + re.escape(token.lower()) + r"(?![a-z0-9])")
        _pattern_cache[token] = pat
    return pat


def _covered(text_norm: str, term: str, aliases: dict) -> bool:
    """True if the term or any of its aliases appears (boundary-aware)."""
    for token in [term, *aliases.get(term, [])]:
        if _pattern(token).search(text_norm):
            return True
    return False


def score(resume_text: str, jd_text: str, rubric: dict) -> dict:
    """Deterministic keyword coverage. Returns {static, dynamic, matched, missing}."""
    terms = rubric.get("keyword_dict", [])
    aliases = rubric.get("aliases", {})
    resume_norm = _norm(resume_text)
    jd_norm = _norm(jd_text)

    in_resume = [t for t in terms if _covered(resume_norm, t, aliases)]
    in_jd = [t for t in terms if _covered(jd_norm, t, aliases)]
    resume_set = set(in_resume)

    static = len(in_resume) / len(terms) if terms else 0.0
    jd_covered = [t for t in in_jd if t in resume_set]
    dynamic = len(jd_covered) / len(in_jd) if in_jd else 1.0
    missing = [t for t in in_jd if t not in resume_set]

    return {"static": static, "dynamic": dynamic, "matched": in_resume, "missing": missing}


def delta(before: dict, after: dict) -> dict:
    """after - before on the two coverage axes (rounded to 10 decimal places)."""
    return {
        "static": round(after["static"] - before["static"], 10),
        "dynamic": round(after["dynamic"] - before["dynamic"], 10),
    }
