from pathlib import Path

from jobmaxxing.discovery.gmail_source import parse_linkedin_alert
from jobmaxxing.normalize import make_dedupe_key

FIXTURE = Path(__file__).parent / "fixtures" / "linkedin_alert.eml"

TITLES = [
    "Machine Learning Engineer Intern - Planning",
    "Software Engineer Intern (Global SRE) - 2026 Summer (BS/MS)",
    "Software Engineer Intern (Ads Infrastructure) - 2026 Summer (BS/MS)",
    "AI Infra Onboard Performance Intern",
    "PhD Software Engineering Intern, Decision Intelligence - Fall 2026",
    "Frontend Software Engineer Intern (Ads Measurement Signal and Privacy) - 2026 Summer (BS/MS)",
]
COMPANIES = ["PlusAI", "TikTok", "TikTok", "XPENG", "NVIDIA", "TikTok"]
LOCATIONS = ["Santa Clara, CA", "San Jose, CA", "San Jose, CA",
             "Santa Clara, CA", "Santa Clara, CA", "San Jose, CA"]
IDS = ["4414359267", "4359034720", "4359064279", "4437144020", "4417503978", "4359022183"]


def test_parse_extracts_six_jobs_from_real_sample():
    recs = parse_linkedin_alert(FIXTURE.read_bytes())
    assert [r.title for r in recs] == TITLES
    assert [r.company for r in recs] == COMPANIES
    assert [r.location for r in recs] == LOCATIONS
    assert [r.external_id for r in recs] == IDS


def test_parse_reconstructs_clean_urls_and_metadata():
    recs = parse_linkedin_alert(FIXTURE.read_bytes())
    assert len(recs) == 6
    for r in recs:
        assert r.url == f"https://www.linkedin.com/jobs/view/{r.external_id}"
        assert "/comm/" not in r.url and "?" not in r.url          # tracking dropped
        assert r.source == "gmail:linkedin-alert"
        assert r.description is None and r.posted_at is None
        assert r.term == ["software engineer intern"]              # header phrase, NOT the subject
        assert r.dedupe_key == make_dedupe_key(r.company, r.title)


def test_parse_decodes_quoted_printable_softwrap():
    # A QP-encoded email: the title wraps across a soft break (20=\r\n26) and a '=' is '=3D'.
    raw = (
        b"From: LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/alternative; boundary="B"\r\n\r\n'
        b"--B\r\n"
        b"Content-Type: text/plain; charset=UTF-8\r\n"
        b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
        b"Your job alert for data scientist in Remote\r\n\r\n"
        b"Data Scientist Intern - 20=\r\n26 Summer\r\n"
        b"Acme\r\n"
        b"Remote\r\n\r\n"
        b"1 connection\r\n"
        b"View job: https://www.linkedin.com/comm/jobs/view/999/?trk=3Dx\r\n"
        b"--B--\r\n"
    )
    recs = parse_linkedin_alert(raw)
    assert len(recs) == 1
    assert recs[0].title == "Data Scientist Intern - 2026 Summer"   # soft-wrap collapsed
    assert recs[0].url == "https://www.linkedin.com/jobs/view/999"
    assert recs[0].term == ["data scientist"]


def test_parse_skips_block_missing_title():
    # A card with a View-job link + id but fewer than 3 usable lines (no title) is dropped.
    raw = (
        b"From: LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/alternative; boundary="B"\r\n\r\n'
        b"--B\r\n"
        b"Content-Type: text/plain; charset=UTF-8\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n"
        b"Your job alert for swe intern in Remote\r\n\r\n"
        b"Acme\r\n"
        b"View job: https://www.linkedin.com/comm/jobs/view/555/?x=1\r\n"
        b"--B--\r\n"
    )
    assert parse_linkedin_alert(raw) == []


def test_parse_email_with_no_job_cards_returns_empty():
    raw = (
        b"From: LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/alternative; boundary="B"\r\n\r\n'
        b"--B\r\n"
        b"Content-Type: text/plain; charset=UTF-8\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n"
        b"Your job alert for swe intern in Remote\r\n\r\nNo jobs this time.\r\n"
        b"--B--\r\n"
    )
    assert parse_linkedin_alert(raw) == []


def test_parse_junk_email_returns_empty():
    assert parse_linkedin_alert(b"not a MIME message at all") == []
