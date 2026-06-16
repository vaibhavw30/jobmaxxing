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

    def __post_init__(self) -> None:
        # Trim surrounding whitespace so a stray leading/trailing space in a
        # scraped company/title (seen live, e.g. " MAG Aerospace") never reaches
        # the DB or the triage UI. Central chokepoint: every adapter builds a
        # JobRecord, so no source can skip this. Guarded by isinstance to stay
        # consistent with the adapters' defensive-parsing style.
        if isinstance(self.company, str):
            self.company = self.company.strip()
        if isinstance(self.title, str):
            self.title = self.title.strip()
