from datetime import datetime, timezone

from ..models import JobRecord
from ..normalize import make_dedupe_key, parse_term


def _clean_str(value) -> str | None:
    """A trimmed non-empty string, or None (non-strings become None). Mirrors the ATS
    adapter so both sources reject blank/whitespace-only company/title the same way."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def parse_simplify_format(
    payload: list[dict], source: str, allowed_years: set[int] | None = None
) -> list[JobRecord]:
    """Parse a Simplify-format listings.json (Simplify / vanshb03 / pitt-csc forks).

    Defensive by design: a single malformed entry is skipped rather than aborting the
    whole feed (fail-soft). Skips entries that are not dicts or are missing
    company_name, title, or url; tolerates dirty `locations` (nulls/non-strings) and
    non-numeric `date_posted`. URLs are stored as-is here; canonicalization happens once
    in the pipeline (the single chokepoint before storage), not per adapter.

    Term filtering: if ``allowed_years`` is provided, entries whose ``terms`` list
    contains only parseable year(s) outside the window are dropped. Entries with no
    parseable terms (N/A, blank, missing) are kept with ``term=[]``. If
    ``allowed_years`` is None, all entries are kept and all parseable terms are tagged.
    """
    records: list[JobRecord] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        company = _clean_str(entry.get("company_name"))
        title = _clean_str(entry.get("title"))
        url = entry.get("url")
        if not company or not title or not url:
            continue

        raw_terms = entry.get("terms")
        raw_terms = raw_terms if isinstance(raw_terms, list) else []
        parsed = []  # (year, original_string) for each parseable term
        for t in raw_terms:
            pt = parse_term(t)
            if pt:
                parsed.append((pt[1], t.strip()))
        if allowed_years is None:
            in_window = [orig for (_year, orig) in parsed]
        else:
            in_window = [orig for (year, orig) in parsed if year in allowed_years]
            if parsed and not in_window:
                continue  # purely off-window: never stored
        term = list(dict.fromkeys(in_window))  # order-preserving de-dup; [] when untagged

        locations = entry.get("locations")
        if isinstance(locations, list):
            location = ", ".join(str(x) for x in locations if x is not None) or None
        else:
            location = None

        posted_at = None
        epoch = entry.get("date_posted")
        # bool is a subclass of int; exclude it. Require a positive numeric epoch.
        if isinstance(epoch, (int, float)) and not isinstance(epoch, bool) and epoch > 0:
            posted_at = datetime.fromtimestamp(epoch, tz=timezone.utc)

        active_val = entry.get("active")
        # absent key -> active by default; explicit null -> treat as unknown -> active.
        is_active = bool(active_val) if active_val is not None else True

        records.append(
            JobRecord(
                source=source,
                company=company,
                title=title,
                url=url,
                external_id=entry.get("id"),
                location=location,
                posted_at=posted_at,
                is_active=is_active,
                dedupe_key=make_dedupe_key(company, title),
                term=term,
            )
        )
    return records
