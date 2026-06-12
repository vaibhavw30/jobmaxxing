import json
import re

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


def parse_tiebreaker_response(text, allowed_types: list[str]):
    """Validate the LLM reply. Return (type, confidence) only if it is strict and in-enum; else None."""
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
