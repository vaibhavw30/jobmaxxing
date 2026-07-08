"""Gmail LinkedIn-alert discovery source — a local, operator-run worker (residential inbox over IMAP).

parse_linkedin_alert is a pure, defensive adapter (no imaplib/network). Only _imap_fetch touches the
network, so this module imports with nothing beyond the base install and tests never open a socket.
"""

import email
import logging
import os
import re

from ..models import JobRecord
from ..normalize import make_dedupe_key
from ..pipeline import ingest_records

logger = logging.getLogger(__name__)

SOURCE = "gmail:linkedin-alert"

_SEPARATOR = re.compile(r"-{10,}")
_TERM = re.compile(r"Your job alert for (.+?) in ", re.IGNORECASE)
_JOBID = re.compile(r"jobs/view/(\d+)")
_HEADER_LINE = re.compile(r"^Your job alert for", re.IGNORECASE)
_SOCIAL = re.compile(r"^\d+\s+(school alumni|alumnus|connections?)\b", re.IGNORECASE)
_VIEWJOB = re.compile(r"View job:", re.IGNORECASE)


def _plain_text(raw_email: bytes) -> str | None:
    """Return the decoded text/plain part of a MIME message, or None if absent. Handles
    quoted-printable + charset. Never raises on malformed input (defensive: junk yields None or an
    empty/no-cards string, which parse_linkedin_alert then resolves to no records)."""
    try:
        msg = email.message_from_bytes(raw_email)
    except Exception:  # message_from_bytes is lenient, but stay defensive
        return None
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return None


def _extract_term(text: str) -> list[str] | None:
    """The saved-search phrase from the digest header ('Your job alert for <phrase> in …'), or None."""
    m = _TERM.search(text)
    if not m:
        return None
    phrase = m.group(1).strip()
    return [phrase] if phrase else None


def parse_linkedin_alert(raw_email: bytes) -> list[JobRecord]:
    """Parse a LinkedIn job-alert email's text/plain body into JobRecords (defensive/fail-soft).
    Each block containing a 'View job:' link + a jobs/view/<id> becomes one record; blocks missing an
    id or with fewer than 3 usable (non-header/social/view) lines are skipped."""
    text = _plain_text(raw_email)
    if not text:
        return []
    term = _extract_term(text)
    records = []
    for block in _SEPARATOR.split(text):
        if not _VIEWJOB.search(block):
            continue
        jid = _JOBID.search(block)
        if not jid:
            continue
        jobid = jid.group(1)
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        kept = [ln for ln in lines
                if not _HEADER_LINE.match(ln)
                and not _SOCIAL.match(ln)
                and not _VIEWJOB.search(ln)]
        if len(kept) < 3:
            continue
        title, company, location = kept[-3], kept[-2], kept[-1]
        records.append(JobRecord(
            source=SOURCE,
            company=company,
            title=title,
            url=f"https://www.linkedin.com/jobs/view/{jobid}",
            external_id=jobid,
            location=location,
            description=None,
            posted_at=None,
            term=term,
            dedupe_key=make_dedupe_key(company, title),
        ))
    return records


def load_gmail_config() -> dict:
    """Read Gmail IMAP settings from the environment (mirrors sheets/client.py). Missing required
    GMAIL_ADDRESS / GMAIL_APP_PASSWORD -> RuntimeError."""
    address = os.environ.get("GMAIL_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    if not address or not app_password:
        raise RuntimeError(
            "GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set (Gmail App Password over IMAP). "
            "See the 'Gmail LinkedIn alerts' section of the README.")
    return {
        "address": address,
        "app_password": app_password,
        "sender": os.environ.get("GMAIL_ALERT_SENDER", "jobalerts-noreply@linkedin.com"),
        "since_days": int(os.environ.get("GMAIL_SINCE_DAYS", "7")),
        "host": os.environ.get("GMAIL_IMAP_HOST", "imap.gmail.com"),
    }


def discover_gmail_alerts(conn, *, fetch, now) -> dict:
    """Fetch raw alert emails via the injected fetch fn, parse + ingest each. Fail-soft per message:
    a parse/ingest error on one email is caught, logged, recorded; the rest still process."""
    raw_msgs = fetch()
    parsed = 0
    errors = []
    for raw in raw_msgs:
        try:
            records = parse_linkedin_alert(raw)
            ingest_records(conn, records, now=now)
            parsed += len(records)
        except Exception as exc:  # fail-soft: one bad email never blocks the rest
            logger.warning("gmail alert message failed: %s", exc)
            errors.append(str(exc))
    return {"messages": len(raw_msgs), "parsed": parsed, "errors": errors}
