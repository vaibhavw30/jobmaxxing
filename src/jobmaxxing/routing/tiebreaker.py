import json
import re

from ..llm.client import LLMUnavailable
from .rules import score_jd
from .types import RouteDecision

_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)


def build_tiebreaker_messages(title: str, description: str, candidates: list[str], config: dict) -> list[dict]:
    """Construct the constrained classification prompt for the tied candidate set."""
    defs = "\n".join(f"- {t}: {config['types'][t].get('definition', '')}" for t in candidates)
    allowed = ", ".join(candidates)
    system = (
        "You are a strict classifier that assigns a job posting to exactly one resume type.\n"
        f"Choose ONLY from these candidate types:\n{defs}\n\n"
        f'Respond with STRICT JSON only: {{"type": <one of: {allowed}>, "confidence": <0.0-1.0>}}. '
        "No prose, no code fences."
    )
    user = f"Title: {title}\n\nJob description:\n{description}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_tiebreaker_response(text: str | None, allowed_types: list[str]):
    """Validate the LLM reply. Return (type, confidence) only if it is strict and in-enum; else None.

    The JSON extractor is greedy (first ``{`` to last ``}``); any ambiguity such as two
    objects or trailing braces fails to parse and returns None — the gate prefers a safe
    reject over a guess, so the caller falls back to the deterministic pick.
    """
    if not isinstance(text, str):
        return None
    match = _JSON_OBJ.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    t = data.get("type")
    c = data.get("confidence")
    if t not in allowed_types:
        return None
    if isinstance(c, bool) or not isinstance(c, (int, float)) or not (0.0 <= c <= 1.0):
        return None
    return (t, float(c))


# Fallback confidence sits below any routing threshold: it marks "the LLM gave no usable
# signal, this is a deterministic best-guess among the candidates", not a confident route.
_FALLBACK_CONFIDENCE = 0.4


def resolve(candidates: list[str], title, description, *, llm_complete, config) -> RouteDecision:
    """Resolve an ambiguous posting with one schema-gated LLM call, choosing among `candidates`.

    On a valid in-enum reply -> RouteDecision(method='llm'). On any parse failure,
    out-of-enum answer, or LLMUnavailable -> deterministic fallback to the highest-JD
    candidate, recorded as method='rules' (the LLM did not decide). Empty candidates ->
    defer. Exceptions other than LLMUnavailable propagate: the batch loop (route_new)
    isolates them per row, so one bad posting never aborts the run.
    """
    if not candidates:
        return RouteDecision(resume_type=None, method=None, confidence=0.0)  # nothing to decide -> defer
    messages = build_tiebreaker_messages(title, description, candidates, config)
    try:
        text = llm_complete("route", messages, max_tokens=200, response_format={"type": "json_object"})
    except LLMUnavailable:
        text = None

    parsed = parse_tiebreaker_response(text, candidates) if text is not None else None
    if parsed is not None:
        resume_type, confidence = parsed
        return RouteDecision(resume_type=resume_type, method="llm", confidence=confidence)

    jd = score_jd(description, config)
    best = max(candidates, key=lambda t: jd.get(t, 0.0))
    return RouteDecision(resume_type=best, method="rules", confidence=_FALLBACK_CONFIDENCE)
