# Gmail LinkedIn-alert parser — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local, operator-run `python -m jobmaxxing.discover_gmail` worker that reads LinkedIn saved-search job-alert emails over IMAP and ingests the listed postings into the shared `jobs` table as link-only rows.

**Architecture:** A pure, defensive `parse_linkedin_alert(raw_email)` adapter turns a raw alert email's `text/plain` part into `JobRecord`s; a fail-soft `discover_gmail_alerts(conn, *, fetch, now)` worker iterates raw messages from an **injected** `fetch` and ingests via the existing `ingest_records`. Only a thin `_imap_fetch` touches `imaplib`/the network, so the parser and worker are fully unit/integration-tested with a scrubbed fixture + fake fetch, and tests never open a socket. Everything is Python stdlib.

**Tech Stack:** Python 3.12, stdlib `imaplib` + `email` + `re`, psycopg3, pytest + pytest-postgresql. **No new dependency.**

## Global Constraints

- Python **3.12**; run pytest with the Postgres binary on PATH:
  `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH"` then `uv run pytest ...`.
- **All stdlib:** `imaplib` (fetch) + `email` (MIME) + `re` (parse). **No new dependency, NO
  `pyproject.toml` change, NO extra.** The module imports with nothing beyond the base install.
- **Local-only:** NO `.github/workflows` change. The App Password grants full IMAP inbox access → it
  stays in the operator's local `.env`, never in CI. Runs on the operator's machine + the nightly scheduler.
- **Reuse, don't reinvent:** `JobRecord` (`models.py`, already whitespace-strips company/title in
  `__post_init__`), `make_dedupe_key` (`normalize.py`), `ingest_records` (`pipeline.py`, signature
  `ingest_records(conn, records, now) -> dict[str,int]`). Mirror `discovery/jobspy_source.py`'s `main`/shim
  and the `sheets/client.py` `os.environ.get` config pattern.
- **Source token:** `source = "gmail:linkedin-alert"` — a stable, lowercase, colon-namespaced token like
  `jobspy:indeed`. It must stay a token (not free-form): `merge.py::_is_ats` keys off the prefix, so a
  non-ATS token correctly stays lower-primacy than a real ATS source.
- **URL reconstruction:** build `https://www.linkedin.com/jobs/view/<id>` from the job id alone (drops the
  `/comm/` tracking redirect + all query params). `canonicalize_url` leaves this query-less linkedin.com
  path untouched (already covered by `tests/test_normalize_url.py::test_linkedin_path_identity_unaffected`).
- **No live IMAP in tests:** the `fetch` boundary is injected. `_imap_fetch` itself is the thin, untested
  side-effect, validated by the operator's first real run (same stance as JobSpy's `_jobspy_scrape`).
- **Scrubbed fixture only:** the committed `tests/fixtures/linkedin_alert.eml` is authored in Task 1 with
  PUBLIC data only. The raw operator email is git-ignored (`/*.eml`) and must NEVER be committed.
- **Worktree cwd discipline:** every command runs in the feature worktree. Begin each Bash command with
  `cd <worktree> && …` and, before any `git commit`, verify `git rev-parse --show-toplevel` is the
  worktree (never the sibling `main` checkout).
- Push with the `vaibhavw30` gh account.

---

## Task 1: scrubbed fixture + `parse_linkedin_alert` pure adapter

**Files:**
- Create: `tests/fixtures/linkedin_alert.eml`
- Create: `src/jobmaxxing/discovery/gmail_source.py`
- Test: `tests/test_gmail_parse.py`

**Interfaces:**
- Consumes: `JobRecord` (`..models`), `make_dedupe_key` (`..normalize`).
- Produces: `parse_linkedin_alert(raw_email: bytes) -> list[JobRecord]`. Walks to the `text/plain` MIME
  part (decoded per its charset — handles quoted-printable), extracts the saved-search phrase →
  `term=[phrase]`, splits the body on `-{10,}` separator runs, and for each block containing a `View job:`
  link + a `jobs/view/<id>` id emits a `JobRecord(source="gmail:linkedin-alert", …, description=None,
  posted_at=None)`. Blocks missing an id or with fewer than 3 usable lines are skipped (fail-soft).

- [ ] **Step 1: Create the scrubbed fixture `tests/fixtures/linkedin_alert.eml`.**
  Write this file VERBATIM. It is authored from the operator's real sample with PUBLIC data only (titles,
  companies, locations, and the public `jobs/view/<id>` ids of real postings); every tracking token,
  the recipient address, the "intended for" bio, and the ~3,400-line HTML body have been removed. The
  header line deliberately runs straight into `Manage your job alerts:` (no break) to reproduce the real
  quoted-printable run-on that the term regex must survive; the first job card sits in the same block as
  the digest header (the first separator comes after it) to exercise the "last-three lines" rule.

  ```text
  From: LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>
  Subject: Machine Learning Engineer Intern - Planning at PlusAI
  To: Operator Example <operator@example.com>
  Date: Tue, 7 Jul 2026 19:18:38 +0000 (UTC)
  MIME-Version: 1.0
  Content-Type: multipart/alternative; boundary="----=_Part_SCRUBBED"
  X-LinkedIn-Class: SAVEDSEARCH
  X-LinkedIn-Template: email_job_alert_digest_01

  ------=_Part_SCRUBBED
  Content-Type: text/plain;charset=UTF-8
  Content-Transfer-Encoding: 8bit
  Content-ID: text-body

  Your job alert for software engineer intern in San Francisco Bay AreaManage your job alerts:  https://www.linkedin.com/comm/jobs/alerts?trk=REDACTED

  Machine Learning Engineer Intern - Planning
  PlusAI
  Santa Clara, CA

  4 school alumni
  View job: https://www.linkedin.com/comm/jobs/view/4414359267/?trackingId=REDACTED&midToken=REDACTED&eid=REDACTED&otpToken=REDACTED

  ---------------------------------------------------------

  Software Engineer Intern (Global SRE) - 2026 Summer (BS/MS)
  TikTok
  San Jose, CA

  216 school alumni
  View job: https://www.linkedin.com/comm/jobs/view/4359034720/?trackingId=REDACTED&midToken=REDACTED&eid=REDACTED

  ---------------------------------------------------------

  Software Engineer Intern (Ads Infrastructure) - 2026 Summer (BS/MS)
  TikTok
  San Jose, CA

  216 school alumni
  View job: https://www.linkedin.com/comm/jobs/view/4359064279/?trackingId=REDACTED&midToken=REDACTED&eid=REDACTED

  ---------------------------------------------------------

  AI Infra Onboard Performance Intern
  XPENG
  Santa Clara, CA

  18 school alumni
  View job: https://www.linkedin.com/comm/jobs/view/4437144020/?trackingId=REDACTED&midToken=REDACTED&eid=REDACTED

  ---------------------------------------------------------

  PhD Software Engineering Intern, Decision Intelligence - Fall 2026
  NVIDIA
  Santa Clara, CA

  3 connections
  View job: https://www.linkedin.com/comm/jobs/view/4417503978/?trackingId=REDACTED&midToken=REDACTED&eid=REDACTED

  ---------------------------------------------------------

  Frontend Software Engineer Intern (Ads Measurement Signal and Privacy) - 2026 Summer (BS/MS)
  TikTok
  San Jose, CA

  2 connections
  View job: https://www.linkedin.com/comm/jobs/view/4359022183/?trackingId=REDACTED&midToken=REDACTED&eid=REDACTED

  ---------------------------------------------------------

  See all jobs on LinkedIn:  https://www.linkedin.com/comm/jobs/search?keywords=software+engineer+intern

  Job search smarter with Premium
  https://www.linkedin.com/comm/premium/products/?upsellOrderOrigin=REDACTED

  ----------------------------------------

  This email was intended for Operator Example (Operator)
  You are receiving Job Alert emails.

  Manage your job alerts:  https://www.linkedin.com/comm/jobs/alerts?trk=REDACTED
  Unsubscribe: https://www.linkedin.com/job-alert-email-unsubscribe?savedSearchId=REDACTED

  (c) 2026 LinkedIn Corporation, 1000 West Maude Avenue, Sunnyvale, CA 94085.
  LinkedIn and the LinkedIn logo are registered trademarks of LinkedIn.
  ------=_Part_SCRUBBED
  Content-Type: text/html;charset=UTF-8
  Content-Transfer-Encoding: 8bit
  Content-ID: html-body

  <html><body>Scrubbed HTML part - not parsed by this worker.</body></html>
  ------=_Part_SCRUBBED--
  ```

- [ ] **Step 2: Write the failing unit tests.** Create `tests/test_gmail_parse.py`:
  ```python
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
  ```
  Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_gmail_parse.py -q`
  Expected: FAIL (module/function missing).

- [ ] **Step 3: Implement.** Create `src/jobmaxxing/discovery/gmail_source.py`:
  ```python
  """Gmail LinkedIn-alert discovery source — a local, operator-run worker (residential inbox over IMAP).

  parse_linkedin_alert is a pure, defensive adapter (no imaplib/network). Only _imap_fetch touches the
  network, so this module imports with nothing beyond the base install and tests never open a socket.
  """

  import email
  import logging
  import re

  from ..models import JobRecord
  from ..normalize import make_dedupe_key

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
  ```

- [ ] **Step 4: Run** `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_gmail_parse.py -q` → PASS (6).

- [ ] **Step 5: Commit** (verify worktree cwd first — `git rev-parse --show-toplevel`):
  ```bash
  git add tests/fixtures/linkedin_alert.eml src/jobmaxxing/discovery/gmail_source.py tests/test_gmail_parse.py
  git commit -m "discovery(gmail): parse_linkedin_alert pure adapter + scrubbed fixture

  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
  ```

---

## Task 2: `load_gmail_config` + `discover_gmail_alerts` worker (fail-soft) + integration

**Files:**
- Modify: `src/jobmaxxing/discovery/gmail_source.py` (add `load_gmail_config`, `discover_gmail_alerts`)
- Test: `tests/test_gmail_config.py`, `tests/test_gmail_discover.py`

**Interfaces:**
- Consumes: `parse_linkedin_alert` (Task 1), `ingest_records` (`..pipeline`).
- Produces:
  - `load_gmail_config() -> dict` — reads env via `os.environ`: required `GMAIL_ADDRESS`,
    `GMAIL_APP_PASSWORD`; optional `GMAIL_ALERT_SENDER` (default `jobalerts-noreply@linkedin.com`),
    `GMAIL_SINCE_DAYS` (default `7`), `GMAIL_IMAP_HOST` (default `imap.gmail.com`). Missing a required key →
    `RuntimeError`.
  - `discover_gmail_alerts(conn, *, fetch, now) -> dict` — `raw_msgs = fetch()`; per raw message
    `parse_linkedin_alert` → `ingest_records(conn, records, now=now)`; fail-soft per message; returns
    `{"messages": int, "parsed": int, "errors": list[str]}`.

- [ ] **Step 1: Write the failing tests.** Create `tests/test_gmail_config.py`:
  ```python
  import pytest

  from jobmaxxing.discovery.gmail_source import load_gmail_config


  def test_config_reads_required_and_defaults(monkeypatch):
      monkeypatch.setenv("GMAIL_ADDRESS", "me@gmail.com")
      monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-pw")
      monkeypatch.delenv("GMAIL_ALERT_SENDER", raising=False)
      monkeypatch.delenv("GMAIL_SINCE_DAYS", raising=False)
      monkeypatch.delenv("GMAIL_IMAP_HOST", raising=False)
      cfg = load_gmail_config()
      assert cfg["address"] == "me@gmail.com"
      assert cfg["app_password"] == "app-pw"
      assert cfg["sender"] == "jobalerts-noreply@linkedin.com"
      assert cfg["since_days"] == 7
      assert cfg["host"] == "imap.gmail.com"


  def test_config_missing_credentials_raises(monkeypatch):
      monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
      monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
      with pytest.raises(RuntimeError):
          load_gmail_config()
  ```
  Create `tests/test_gmail_discover.py`:
  ```python
  from datetime import datetime, timezone
  from pathlib import Path

  import psycopg
  import pytest

  from jobmaxxing.migrate import apply_migrations
  from jobmaxxing.discovery.gmail_source import discover_gmail_alerts

  FIXTURE = (Path(__file__).parent / "fixtures" / "linkedin_alert.eml").read_bytes()


  @pytest.fixture
  def conn(postgresql):
      dsn = (f"host={postgresql.info.host} port={postgresql.info.port} "
             f"dbname={postgresql.info.dbname} user={postgresql.info.user}")
      with psycopg.connect(dsn) as c:
          apply_migrations(c)
          yield c


  def test_discover_ingests_six_link_only_rows(conn):
      now = datetime(2026, 7, 8, tzinfo=timezone.utc)
      report = discover_gmail_alerts(conn, fetch=lambda: [FIXTURE], now=now)
      assert report["messages"] == 1 and report["parsed"] == 6 and report["errors"] == []
      rows = conn.execute(
          "select source, term, description from jobs order by title"
      ).fetchall()
      assert len(rows) == 6
      assert all(r[0] == "gmail:linkedin-alert" for r in rows)
      assert all(r[1] == ["software engineer intern"] for r in rows)   # term stored
      assert all(r[2] is None for r in rows)                           # link-only (no JD)


  def test_discover_dedupes_same_email_across_runs(conn):
      now = datetime(2026, 7, 8, tzinfo=timezone.utc)
      discover_gmail_alerts(conn, fetch=lambda: [FIXTURE], now=now)
      discover_gmail_alerts(conn, fetch=lambda: [FIXTURE, FIXTURE], now=now)  # re-read: idempotent
      assert conn.execute("select count(*) from jobs").fetchone()[0] == 6


  def test_discover_is_failsoft_on_a_bad_message(conn):
      now = datetime(2026, 7, 8, tzinfo=timezone.utc)
      # one good message + one junk message → the good one still ingests, the bad one is recorded
      report = discover_gmail_alerts(conn, fetch=lambda: [FIXTURE, b"junk"], now=now)
      assert report["messages"] == 2
      assert conn.execute("select count(*) from jobs").fetchone()[0] == 6
  ```
  Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_gmail_config.py tests/test_gmail_discover.py -q` → FAIL (functions missing).

  Note on `test_discover_is_failsoft_on_a_bad_message`: `b"junk"` parses to `[]` (no records, no error),
  so it lands in the "messages seen but nothing parsed" path — still fail-soft and still 6 rows. The test
  asserts the good message survives regardless; the per-message try/except also covers a message that
  raises during ingest.

- [ ] **Step 2: Implement.** Add to `src/jobmaxxing/discovery/gmail_source.py`. Add `import os` to the
  imports at the top (alongside `email`, `logging`, `re`), then append:
  ```python
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
  ```
  Also add `from ..pipeline import ingest_records` to the imports at the top of the module.

- [ ] **Step 3: Run** `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_gmail_config.py tests/test_gmail_discover.py tests/test_gmail_parse.py -q` → PASS.

- [ ] **Step 4: Commit** (verify worktree cwd first):
  ```bash
  git add src/jobmaxxing/discovery/gmail_source.py tests/test_gmail_config.py tests/test_gmail_discover.py
  git commit -m "discovery(gmail): load_gmail_config + fail-soft discover_gmail_alerts worker

  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
  ```

---

## Task 3: `_imap_fetch` + `main` + CLI shim + nightly worker + docs

**Files:**
- Modify: `src/jobmaxxing/discovery/gmail_source.py` (add `_imap_fetch`, `main`)
- Create: `src/jobmaxxing/discover_gmail.py` (CLI shim)
- Modify: `src/jobmaxxing/scheduling/nightly.py` (`WORKER_NAMES` — add `discover_gmail` first)
- Test: `tests/test_gmail_nightly.py`
- Modify: `.env.example`, `README.md`

**Interfaces:**
- Consumes: `discover_gmail_alerts`, `load_gmail_config` (Task 2), `load_settings` (`..config`), `psycopg`.
- Produces: `_imap_fetch(*, host, address, app_password, sender, since_days) -> list[bytes]` (the only
  network code); `main() -> None`; `python -m jobmaxxing.discover_gmail`; `discover_gmail` as the first
  entry in `nightly.WORKER_NAMES`.

- [ ] **Step 1: Add a failing nightly-integration test.** Create `tests/test_gmail_nightly.py`:
  ```python
  from jobmaxxing.scheduling.nightly import WORKER_NAMES, default_workers


  def test_discover_gmail_runs_first_in_nightly_batch():
      # pure ingestion → runs before the scrapers/enrichers
      assert WORKER_NAMES[0] == "discover_gmail"
      assert "discover_gmail" in WORKER_NAMES


  def test_default_workers_invoke_discover_gmail_module():
      names = [name for name, _argv in default_workers()]
      assert names[0] == "discover_gmail"
      argv = dict(default_workers())["discover_gmail"]
      assert argv[-2:] == ["-m", "jobmaxxing.discover_gmail"]
  ```
  Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_gmail_nightly.py -q` → FAIL (`discover_gmail` not in WORKER_NAMES yet).

- [ ] **Step 2: Add `discover_gmail` to the nightly batch.** In `src/jobmaxxing/scheduling/nightly.py`,
  change the `WORKER_NAMES` line (currently
  `WORKER_NAMES = ["discover_jobspy", "enrich_workday", "recover_jd", "verify_url"]`) to:
  ```python
  # The residential-IP workers, in dependency order (ingest LinkedIn alerts -> discover -> fill JDs ->
  # recover -> verify). discover_gmail is pure ingestion, so it runs first.
  WORKER_NAMES = ["discover_gmail", "discover_jobspy", "enrich_workday", "recover_jd", "verify_url"]
  ```

- [ ] **Step 3: Run** `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest tests/test_gmail_nightly.py -q` → PASS.

- [ ] **Step 4: Implement `_imap_fetch` + `main`.** Append to `src/jobmaxxing/discovery/gmail_source.py`.
  First add these to the top-of-module imports (alongside the existing `email`/`logging`/`re`/`os` and the
  `..models`/`..normalize`/`..pipeline` imports): `import imaplib`, `import psycopg`,
  `from datetime import datetime, timedelta, timezone`, `from ..config import load_settings`. Then append:
  ```python
  def _imap_fetch(*, host, address, app_password, sender, since_days) -> list[bytes]:
      """The ONLY network code: fetch raw alert emails from Gmail over IMAP (FROM <sender> SINCE now-N
      days). Returns raw RFC822 bytes per message. Untested side-effect (operator validates first run)."""
      since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%d-%b-%Y")
      raw_msgs = []
      imap = imaplib.IMAP4_SSL(host)
      try:
          imap.login(address, app_password)
          imap.select("INBOX")
          typ, data = imap.search(None, "FROM", sender, "SINCE", since)
          if typ != "OK":
              return []
          for num in data[0].split():
              typ, msg_data = imap.fetch(num, "(RFC822)")
              if typ == "OK" and msg_data and msg_data[0]:
                  raw_msgs.append(msg_data[0][1])
      finally:
          try:
              imap.logout()
          except Exception:
              pass
      return raw_msgs


  def main() -> None:
      logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
      settings = load_settings()
      cfg = load_gmail_config()
      now = datetime.now(timezone.utc)
      with psycopg.connect(settings.database_url) as conn:
          report = discover_gmail_alerts(
              conn,
              fetch=lambda: _imap_fetch(
                  host=cfg["host"], address=cfg["address"], app_password=cfg["app_password"],
                  sender=cfg["sender"], since_days=cfg["since_days"]),
              now=now,
          )
      logger.info("gmail discovery report: %s", report)
      print(f"gmail discovery: {report['messages']} messages, {report['parsed']} postings parsed, "
            f"{len(report['errors'])} errors")
  ```

- [ ] **Step 5: Create the CLI shim** `src/jobmaxxing/discover_gmail.py`:
  ```python
  """CLI shim: `python -m jobmaxxing.discover_gmail` (run LOCALLY; needs GMAIL_* env vars)."""

  from .discovery.gmail_source import main

  if __name__ == "__main__":
      main()
  ```

- [ ] **Step 6: Verify the module still imports with only the base install** (no new dependency was
  added). Run: `uv run python -c "import jobmaxxing.discovery.gmail_source as m; print('ok:', hasattr(m, 'parse_linkedin_alert') and hasattr(m, 'main'))"`
  Expected: `ok: True`.

- [ ] **Step 7: Document env vars in `.env.example`.** Append this block:
  ```bash
  # --- Gmail LinkedIn-alert parser (local `discover_gmail` worker) ---
  # Reads your LinkedIn saved-search job-alert emails over IMAP and ingests the postings as link-only
  # rows. Uses a Gmail App Password (NOT your login password), so no OAuth. Local-only — never in CI
  # (an App Password grants full IMAP inbox access). Mint one at https://myaccount.google.com/apppasswords
  # (requires 2-Step Verification). Then enable IMAP in Gmail Settings > Forwarding and POP/IMAP.
  GMAIL_ADDRESS=you@gmail.com
  GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx
  # Optional overrides (defaults shown):
  # GMAIL_ALERT_SENDER=jobalerts-noreply@linkedin.com
  # GMAIL_SINCE_DAYS=7
  # GMAIL_IMAP_HOST=imap.gmail.com
  ```

- [ ] **Step 8: README section.** In `README.md`, near the other local workers (e.g. after the JobSpy
  discovery section), add:
  ```markdown
  ### Gmail LinkedIn alerts (local, operator-run)

  Ingest your LinkedIn **saved-search job-alert emails** into the `jobs` table — the only LinkedIn channel
  the pipeline touches (no logged-in scraping). **Run LOCALLY**: it reads your inbox over IMAP with a Gmail
  App Password, which stays in your local `.env` and never in CI.

  One-time setup:
  1. Create LinkedIn saved searches (one per role) with **daily email alerts**.
  2. Turn on 2-Step Verification, then mint a Gmail **App Password** at
     https://myaccount.google.com/apppasswords and enable IMAP (Gmail Settings > Forwarding and POP/IMAP).
  3. Set `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` in `.env` (see `.env.example`).

  Run:

      uv run python -m jobmaxxing.discover_gmail

  It fetches alert emails from the last `GMAIL_SINCE_DAYS` (default 7), parses each listed posting into a
  link-only row (`source=gmail:linkedin-alert`, `term=<saved-search phrase>`, no JD), and ingests via the
  shared dedupe/upsert. Rows are routable on title and get a real description later if the same job arrives
  from an ATS/GitHub source. Fail-soft (one bad email never blocks the rest) and idempotent (re-reads a
  rolling window each run). It also runs automatically as the first worker in the nightly scheduler.
  ```

- [ ] **Step 9: Full suite (no regression).**
  Run: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest -q`
  Expected: PASS (all; the new gmail tests included; existing nightly tests unaffected — none pinned the
  worker-list length).

- [ ] **Step 10: Commit** (verify worktree cwd first):
  ```bash
  git add src/jobmaxxing/discovery/gmail_source.py src/jobmaxxing/discover_gmail.py \
          src/jobmaxxing/scheduling/nightly.py tests/test_gmail_nightly.py .env.example README.md
  git commit -m "discovery(gmail): imap fetch + entrypoint + nightly worker + docs

  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
  ```

---

## Verification (end to end)
1. Full suite: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run pytest -q` → green.
2. Module imports with only the base install (Task 3 Step 6 command) → `ok: True`.
3. No raw email in git: `git status --porcelain | grep -i '\.eml$'` shows only
   `tests/fixtures/linkedin_alert.eml` (never a root-level `.eml`); `git check-ignore '*.eml'` at repo
   root is honored.
4. Optional live run (operator, real inbox + DB): set `GMAIL_ADDRESS`/`GMAIL_APP_PASSWORD`, then
   `uv run python -m jobmaxxing.discover_gmail` → logs a per-run report; new `gmail:linkedin-alert` rows
   appear in triage with clean `linkedin.com/jobs/view/<id>` links and the saved-search phrase in `term`.

## Risks & notes
- **Format drift** — LinkedIn could change the plain-text layout. The parser degrades to "fewer rows,"
  never a crash (defensive block skipping); a real change surfaces as a parse test to update against a
  fresh sample.
- **`_imap_fetch` untested** — the injected boundary keeps everything else unit/integration-tested; the
  thin IMAP call is validated by the operator's first live run (same stance as JobSpy's `_jobspy_scrape`).
- **Link-only rows** — `description=None` by design; enriched later only if the same `company|title`
  arrives from an ATS/GitHub source (merge fills the JD, ATS becomes primary, the LinkedIn URL survives in
  `alt_urls`). Meanwhile routable on title.
- **Soft dedupe by `company|title`** — project-wide behavior (accepted), so a company's multiple
  same-titled alerts collapse to one row; cross-source collapse with other sources is intended.
- **Idempotency** — re-scans a rolling `SINCE now-7d` window each run; re-reading emails just re-upserts.

## Execution
Isolated git worktree; subagent-driven TDD, one task per subagent (implementer reads `models.py`,
`normalize.py`, `pipeline.py`, and `discovery/jobspy_source.py` for context first); two-stage review
(spec → quality) per task; full-suite green; whole-branch review; merge to `main`; push (gh `vaibhavw30`).
