from datetime import datetime, timezone

from jobmaxxing.models import JobRecord


def test_jobrecord_defaults():
    rec = JobRecord(source="github:simplify", company="Acme", title="SWE Intern", url="https://x/y")
    assert rec.alt_urls == []
    assert rec.is_active is True
    assert rec.external_id is None
    assert rec.dedupe_key == ""


def test_jobrecord_strips_surrounding_whitespace_from_company_and_title():
    rec = JobRecord(
        source="github:simplify",
        company="  CCC Intelligent Solutions ",
        title="\tSoftware Engineer Intern\n",
        url="https://x/y",
    )
    assert rec.company == "CCC Intelligent Solutions"
    assert rec.title == "Software Engineer Intern"


def test_jobrecord_accepts_all_fields():
    rec = JobRecord(
        source="greenhouse",
        company="Acme",
        title="SWE Intern",
        url="https://x/y",
        external_id="123",
        location="NYC",
        description="JD text",
        posted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        is_active=False,
        alt_urls=["https://a"],
        dedupe_key="acme|swe intern",
    )
    assert rec.external_id == "123"
    assert rec.alt_urls == ["https://a"]
