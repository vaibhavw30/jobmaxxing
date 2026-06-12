from dataclasses import dataclass, field

VALID_TYPES = (
    "quant-trader",
    "quant-dev",
    "mle",
    "swe",
    "fdse",
    "ai",
    "robotics",
    "av",
)


@dataclass
class RulesOutcome:
    """Result of the deterministic rules pass."""

    decision: str  # "routed" | "ambiguous" | "no_signal"
    resume_type: str | None = None
    confidence: float = 0.0
    candidates: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RouteDecision:
    """Final routing decision for one posting. resume_type/method None => defer.

    Frozen so the shared `_DEFER` singleton in route.py can be returned from every defer
    path without any caller being able to mutate it.
    """

    resume_type: str | None
    method: str | None  # "rules" | "llm" | None
    confidence: float = 0.0


@dataclass
class Budget:
    """Remaining LLM calls allowed this run."""

    remaining: int
