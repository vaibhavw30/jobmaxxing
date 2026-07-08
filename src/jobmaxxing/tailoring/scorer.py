import json
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
    alias_list = aliases.get(term, [])
    if not isinstance(alias_list, list):
        alias_list = []  # guard: a malformed (non-list) alias value must not spread into chars
    for token in [term, *alias_list]:
        if _pattern(token).search(text_norm):
            return True
    return False


def score_keywords(resume_text: str, jd_text: str, rubric: dict) -> dict:
    """Deterministic keyword coverage. Returns {static, dynamic, matched, missing}.

    - static:  fraction of the rubric's keyword_dict terms present in the résumé.
    - dynamic: of the dict terms the JD mentions, the fraction the résumé also covers
               (1.0 if the JD mentions none of the dict terms).
    - matched: ALL dict terms present in the résumé (JD-independent).
    - missing: dict terms the JD mentions but the résumé lacks (JD-conditioned — the
               complement of `matched` within the JD's terms, NOT of the whole dict).
    """
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


_AXES = ("keyword_coverage", "technical_depth", "impact", "ats", "relevance_order")


def score(resume_text: str, jd_text: str, rubric: dict, *, complete) -> dict:
    """Full five-axis score: deterministic keyword coverage + four LLM-graded axes + weighted composite.
    A superset of score_keywords() (static/dynamic/matched/missing preserved), plus `axes` and `composite`."""
    kw = score_keywords(resume_text, jd_text, rubric)
    qual = score_qualitative(resume_text, jd_text, rubric, complete=complete)
    axes = {"keyword_coverage": round(10 * kw["dynamic"], 4), **qual}
    weights = rubric.get("weights") or {a: 0.2 for a in _AXES}
    composite = round(sum(weights.get(a, 0.0) * axes[a] for a in _AXES), 2)
    return {**kw, "axes": axes, "composite": composite}


def delta(before: dict, after: dict) -> dict:
    """after - before: keyword coverage (static/dynamic), the composite, and each 0-10 axis."""
    out = {
        "static": round(after["static"] - before["static"], 10),
        "dynamic": round(after["dynamic"] - before["dynamic"], 10),
        "composite": round(after.get("composite", 0.0) - before.get("composite", 0.0), 2),
    }
    ba, aa = before.get("axes", {}), after.get("axes", {})
    out["axes"] = {a: round(aa.get(a, 0.0) - ba.get(a, 0.0), 2) for a in _AXES}
    return out


_QUAL_AXES = ("technical_depth", "impact", "ats", "relevance_order")

_SCORE_SYSTEM = (
    "You score a LaTeX résumé against a job description on four axes, each an integer 0-10 (10 = ideal):\n"
    "- technical_depth: does the résumé show the LEVEL the role wants, not just the noun?\n"
    "- impact: bullets carrying real numbers / measurable outcomes.\n"
    "- ats: clean structure, standard section headers, no parser-breaking formatting.\n"
    "- relevance_order: most JD-relevant experience surfaced first.\n"
    "Anchor to the role's key terms provided. Respond with STRICT JSON only: "
    '{"technical_depth": n, "impact": n, "ats": n, "relevance_order": n}.'
)


def parse_qualitative(text) -> dict:
    """Lenient parse of the four 0-10 axes. Clamps to [0, 10]; ANY structural failure (no JSON, bad or
    missing axis) -> all four = 5.0 (neutral), so a flaky scoring call never crashes tailoring."""
    neutral = {a: 5.0 for a in _QUAL_AXES}
    if not isinstance(text, str):
        return dict(neutral)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return dict(neutral)
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return dict(neutral)
    if not isinstance(data, dict):
        return dict(neutral)
    out = {}
    for axis in _QUAL_AXES:
        value = data.get(axis)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return dict(neutral)  # all-or-nothing: a single bad/missing axis -> neutral
        out[axis] = float(min(10, max(0, value)))
    return out


def score_qualitative(resume_text: str, jd_text: str, rubric: dict, *, complete) -> dict:
    """Pass the résumé + JD + the rubric's key terms to the LLM for the four qualitative axes (temp-0)."""
    terms = ", ".join(rubric.get("keyword_dict", []))
    messages = [
        {"role": "system", "content": _SCORE_SYSTEM},
        {"role": "user", "content": f"Role key terms: {terms}\n\nJob description:\n{jd_text}\n\nRésumé (LaTeX):\n{resume_text}"},
    ]
    text = complete("score", messages, max_tokens=300, response_format={"type": "json_object"}, temperature=0)
    return parse_qualitative(text)
