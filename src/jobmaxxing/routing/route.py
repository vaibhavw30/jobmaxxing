from .rules import route_by_rules
from .tiebreaker import resolve
from .types import Budget, RouteDecision

_DEFER = RouteDecision(resume_type=None, method=None, confidence=0.0)
# A single weakly-scoring candidate: route it deterministically (the LLM has nothing to
# disambiguate), with modest confidence.
_SINGLE_CANDIDATE_CONFIDENCE = 0.5


def route_one(title, description, config, *, llm_complete, budget: Budget) -> RouteDecision:
    """Route a single posting. Title-first deterministic; the LLM is used only for
    ambiguous JD-bearing rows with >1 candidate, within budget; otherwise defer."""
    outcome = route_by_rules(title, description, config)
    if outcome.decision == "routed":
        return RouteDecision(resume_type=outcome.resume_type, method="rules", confidence=outcome.confidence)
    if outcome.decision == "no_signal":
        return _DEFER
    # ambiguous
    if len(outcome.candidates) == 1:
        return RouteDecision(resume_type=outcome.candidates[0], method="rules", confidence=_SINGLE_CANDIDATE_CONFIDENCE)
    if not description:
        return _DEFER  # title-only ambiguity: defer until a JD arrives
    if budget.remaining <= 0:
        return _DEFER  # per-run LLM cap reached
    budget.remaining -= 1
    return resolve(outcome.candidates, title, description, llm_complete=llm_complete, config=config)
