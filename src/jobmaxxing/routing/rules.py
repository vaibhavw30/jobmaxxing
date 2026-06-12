import re

_WS = re.compile(r"\s+")
_pattern_cache: dict[str, re.Pattern] = {}


def _norm(text: str) -> str:
    """Lowercase and collapse whitespace; keep punctuation (tech terms need it)."""
    return _WS.sub(" ", text.lower()).strip()


def _boundary_pattern(signal: str) -> re.Pattern:
    """A signal matched only when not flanked by alphanumerics, so 'ai' != 'training'
    but 'c++' still matches. Cached per signal."""
    pat = _pattern_cache.get(signal)
    if pat is None:
        pat = re.compile(r"(?<![a-z0-9])" + re.escape(signal.lower()) + r"(?![a-z0-9])")
        _pattern_cache[signal] = pat
    return pat


def _count_hits(text: str, signals: list[str]) -> int:
    """Count distinct signals appearing as boundary-delimited substrings of `text` (already
    normalized). De-duplicated so an accidental repeat in the config can't double-count."""
    return sum(1 for s in set(signals) if _boundary_pattern(s).search(text))


def score_title(title: str, config: dict) -> dict[str, int]:
    """title_hits per type."""
    norm = _norm(title or "")
    return {t: _count_hits(norm, spec.get("title_signals", [])) for t, spec in config["types"].items()}


def score_jd(description: str | None, config: dict) -> dict[str, float]:
    """jd_hits (capped) minus exclusion hits, per type. Empty description -> all zeros.

    The cap bounds only the positive signal (so a keyword-stuffed JD can't dominate);
    exclusions are then subtracted, so a heavily-excluded type can go NEGATIVE. That is
    intentional — an exclusion actively disqualifies a type. Task 7 treats `best <= 0`
    as "no usable JD signal", so negative scores degrade to defer/ambiguous, never a route.
    """
    if not description:
        return {t: 0.0 for t in config["types"]}
    norm = _norm(description)
    cap = config.get("thresholds", {}).get("jd_hits_cap", 5)
    out: dict[str, float] = {}
    for t, spec in config["types"].items():
        hits = min(_count_hits(norm, spec.get("jd_signals", [])), cap)
        excl = _count_hits(norm, spec.get("exclude_signals", []))
        out[t] = float(hits) - float(excl)
    return out
