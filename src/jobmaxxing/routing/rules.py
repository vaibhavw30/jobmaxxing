import re

from .types import RulesOutcome

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


def _margin_ratio(top: float, second: float) -> float:
    return max(0.0, min(1.0, (top - second) / max(top, 1.0)))


def _rank(scores: dict[str, float]) -> tuple[str, float, float]:
    """Return (best_type, best_score, second_score). Ties broken alphabetically so the
    result is deterministic regardless of config key order. Empty dict -> ("", 0.0, 0.0)."""
    if not scores:
        return ("", 0.0, 0.0)
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    best_type, best = ordered[0]
    second = ordered[1][1] if len(ordered) > 1 else 0.0
    return best_type, best, second


def route_by_rules(title: str | None, description: str | None, config: dict) -> RulesOutcome:
    """Deterministic routing decision (spec §6.2). Title signals are authoritative."""
    thr = config.get("thresholds", {})
    min_margin = thr.get("min_margin_ratio", 0.5)
    min_top = thr.get("min_top_score", 1.0)

    title_hits = score_title(title, config)
    titled = [t for t, h in title_hits.items() if h > 0]

    # (1) exactly one title match -> route deterministically, title is authoritative.
    if len(titled) == 1:
        t = titled[0]
        confidence = min(1.0, 0.7 + 0.1 * (title_hits[t] - 1))
        return RulesOutcome(decision="routed", resume_type=t, confidence=confidence)

    jd = score_jd(description, config)

    # (2) multiple title matches -> break the tie using JD margin among those candidates.
    if len(titled) > 1:
        cand_scores = {t: jd[t] for t in titled}
        best_t, best, second = _rank(cand_scores)
        margin = _margin_ratio(best, second)
        if best > 0 and margin > min_margin:
            return RulesOutcome(decision="routed", resume_type=best_t, confidence=margin)
        return RulesOutcome(decision="ambiguous", candidates=sorted(titled))

    # (3) no title signal -> route from JD alone if a clear winner, else ambiguous/no_signal.
    best_t, best, second = _rank(jd)
    if best <= 0:
        return RulesOutcome(decision="no_signal")
    margin = _margin_ratio(best, second)
    if best >= min_top and margin > min_margin:
        return RulesOutcome(decision="routed", resume_type=best_t, confidence=margin)
    candidates = sorted([t for t, s in jd.items() if s > 0])
    return RulesOutcome(decision="ambiguous", candidates=candidates)
