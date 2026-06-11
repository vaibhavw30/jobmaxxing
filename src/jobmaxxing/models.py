from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class JobRecord:
    """A normalized posting as produced by a source adapter."""

    source: str
    company: str
    title: str
    url: str
    external_id: str | None = None
    location: str | None = None
    description: str | None = None
    posted_at: datetime | None = None
    is_active: bool = True
    alt_urls: list[str] = field(default_factory=list)
    dedupe_key: str = ""
