# Gmail LinkedIn-alert parser — design

## Context
Phase 5 (broader discovery), the remaining additive source. The operator's LinkedIn **saved-search job
alerts** arrive as email in their Gmail. This worker reads those emails over **IMAP** and ingests the
listed postings into the shared `jobs` table as link-only rows — the only LinkedIn channel the pipeline
touches (no logged-in LinkedIn scraping; ban risk). It is additive/fail-soft: nothing else depends on it.

**Auth decision (resolved via research + live check):** the operator's Gmail is a Family-Link account but
in the *exception* bucket (App Passwords generator present, IMAP tab present), so access is a **Gmail App
Password over IMAP** — a static `.env` credential, no OAuth, no restricted-scope verification, no 7-day
token expiry. This deliberately avoids the Google-OAuth wall that killed the earlier Sheets integration
(and Gmail API `gmail.readonly` is a restricted scope with exactly that friction — rejected).

**Format decision (from a real sample `.eml`):** LinkedIn alert emails are `multipart/alternative`; the
**`text/plain`** part is cleanly structured and far more stable than the nested HTML, so it is the parse
target. Everything is **stdlib** — `imaplib` (fetch) + `email` (MIME) + `re` (parse). **No new
dependency, no `pyproject.toml` change, no `gmail` extra.**

**Confirmed against the real sample (`Machine Learning Engineer Intern - Planning at PlusAI.eml`, 6 job
cards):** four precise details, folded into the design below.
1. **The `text/plain` part is `quoted-printable`.** It MUST be decoded via the email lib
   (`part.get_payload(decode=True).decode(charset)`), never read as raw lines — job cards wrap across QP
   soft breaks (e.g. a title `… - 20=\n26 Summer (BS/MS)` decodes to one line; the header line ends `Area=\n`
   and continues into `Manage your job alerts`). Decoding collapses all of these before parsing.
2. **Term regex** = `Your job alert for (.+?) in ` → `"software engineer intern"` (the header runs straight
   into `Manage your job alerts:` with no separator, so match non-greedily up to ` in `). Confirms the
   subject (`…at PlusAI`) is NOT the term.
3. **The first block bleeds the digest header + job #1** (the first `-{10,}` separator comes *after* job 1).
   The "last three non-excluded lines → title/company/location" rule handles it: after excluding the
   `^Your job alert for` line, the social-proof line, and the `View job:` line, exactly title/company/
   location remain.
4. **Fixture scrubbing (supersedes "lightly scrubbed footer" below).** The committed
   `tests/fixtures/linkedin_alert.eml` is a **minimal, faithful, scrubbed** email: keep the real
   `text/plain` structure + all 6 cards' *public* data (titles, companies, locations, `jobs/view/<id>`
   ids) + the QP quirks (incl. the job-6 title wrap and the header→"Manage" run-on) + a representative
   `?trackingId=…&midToken=…&otpToken=…` query on each `View job:` URL with **neutered dummy values** (so
   the parser still proves it strips the `/comm/` redirect and the whole query). STRIP: every
   crypto/routing header (DKIM-Signature, ARC-*, Received, Return-Path bounce address, Feedback-ID,
   X-LinkedIn-fbl/-Id, List-Unsubscribe token, Require-Recipient-Valid-Since); replace the recipient
   address (`To`/`Delivered-To`) with `operator@example.com`; replace the "This email was intended for
   <name> (<bio>)" footer line with a generic placeholder; replace the ~3,400-line `text/html` part with a
   one-line stub. Job-IDs and company names are PUBLIC posting data (not operator PII), so they stay —
   they are the ground truth the tests assert on. Keep a valid `multipart/alternative` structure
   (text/plain + stub text/html) so `email.message_from_bytes` walks it exactly like the real thing.

## Goal
`python -m jobmaxxing.discover_gmail` — a local, operator-run, fail-soft worker that fetches recent
LinkedIn job-alert emails over IMAP, parses each listed posting into a `JobRecord`, and ingests via the
existing `ingest_records` (dedupe + canonicalize + upsert). Also runs automatically as a 5th worker in
the local nightly scheduler. All IMAP/network access is behind an **injected fetch function**, so the
parser is a pure, fully unit-tested adapter and tests never touch a live inbox.

## Design

### The sample (ground truth)
From `LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>`, subject = the first job's title+company,
`List-Unsubscribe` carries `savedSearchId`. The `text/plain` body:
- A header line: `Your job alert for <search phrase> in <search location>` (→ the saved-search term).
- Job cards separated by `----------` runs. Each card's non-empty lines are:
  `Title` / `Company` / `Location` / `<N> school alumni|connections` (social proof, optional) /
  `View job: https://www.linkedin.com/comm/jobs/view/<id>/?<tracking>`.
- A footer (see-all-jobs link, premium upsell, "This email was intended for …").

### Components / files
- **`src/jobmaxxing/discovery/gmail_source.py`** (new):
  - `parse_linkedin_alert(raw_email: bytes) -> list[JobRecord]` — **pure, defensive**. Uses
    stdlib `email.message_from_bytes`, walks to the `text/plain` part (decoded per its charset). Extracts
    the header search phrase → `term = [phrase]` (or `None` if absent). Splits the body on separator runs
    (`-{10,}`); for each block containing a `View job:` link:
    - `jobid = re.search(r'jobs/view/(\d+)', block)` → `url = f"https://www.linkedin.com/jobs/view/{jobid}"`,
      `external_id = jobid`. (Reconstructing from the id drops the `/comm/` tracking-redirect and all query
      params; `canonicalize_url` then leaves it untouched.)
    - Collect the block's non-empty stripped lines, EXCLUDING the header line (`^Your job alert for`), any
      social-proof line (`^\d+\s+(school alumni|alumnus|connections?)`), and the `View job:` line. The
      **last three** remaining lines are `title, company, location` (last-three is robust to the digest
      header bleeding into the first block).
    - `JobRecord(source="gmail:linkedin-alert", company, title, url, external_id=jobid, location,
      description=None, posted_at=None, term=term, dedupe_key=make_dedupe_key(company, title))`.
    - Skip any block missing a jobid / title / company (fail-soft); a malformed block never aborts the email.
    Returns `list[JobRecord]`. No IMAP/network import.
  - `_imap_fetch(*, host, address, app_password, sender, since_days) -> list[bytes]` — the **only**
    network code: stdlib `imaplib.IMAP4_SSL(host)`; `login(address, app_password)`; `select("INBOX")`;
    `search(None, 'FROM', sender, 'SINCE', <dd-Mon-yyyy>)`; `fetch(id, "(RFC822)")` per hit; return the
    raw message bytes. Injected as the default fetch, so tests pass a fake and never open a socket.
  - `discover_gmail_alerts(conn, *, fetch, now) -> dict` — call `raw_msgs = fetch()`; for each raw message:
    `records = parse_linkedin_alert(raw)`; `counts = ingest_records(conn, records, now=now)`.
    **Fail-soft per message:** a parse/ingest error on one email is caught, logged, and recorded; the rest
    still process. Returns a per-run report (messages seen, records ingested, errors).
  - `load_gmail_config() -> dict` — reads `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, and optional
    `GMAIL_ALERT_SENDER` (default `jobalerts-noreply@linkedin.com`), `GMAIL_SINCE_DAYS` (default `7`),
    `GMAIL_IMAP_HOST` (default `imap.gmail.com`) via `os.environ` — mirrors how `sheets/client.py` reads
    its env (no `config.py` change). Missing `GMAIL_ADDRESS`/`GMAIL_APP_PASSWORD` → clear `RuntimeError`.
  - `main() -> None` — `logging.basicConfig(...)`; `load_settings()` (DB) + `load_gmail_config()`;
    `psycopg.connect`; `discover_gmail_alerts(conn, fetch=lambda: _imap_fetch(**cfg), now=now)`; log +
    print a one-line summary. Mirrors `discover_jobspy`'s `main`.
- **`src/jobmaxxing/discover_gmail.py`** (new) — CLI shim: `from .discovery.gmail_source import main`.
- **`src/jobmaxxing/scheduling/nightly.py`** — add `"discover_gmail"` to `WORKER_NAMES` (5th worker) so it
  runs in the nightly batch. (Order: it's pure ingestion; place it first, before the scrapers.)
- **`tests/fixtures/linkedin_alert.eml`** — the operator's real sample, **scrubbed per the four-point
  "Fixture scrubbing" rule in Context above** (all PII/tracking headers + recipient + footer name + the
  HTML part removed; the 6 job cards' public data + QP structure intact). The 6 ground-truth jobs are:
  (1) `Machine Learning Engineer Intern - Planning` / PlusAI / Santa Clara, CA / id `4414359267`;
  (2) `Software Engineer Intern (Global SRE) - 2026 Summer (BS/MS)` / TikTok / San Jose, CA / `4359034720`;
  (3) `Software Engineer Intern (Ads Infrastructure) - 2026 Summer (BS/MS)` / TikTok / San Jose, CA / `4359064279`;
  (4) `AI Infra Onboard Performance Intern` / XPENG / Santa Clara, CA / `4437144020`;
  (5) `PhD Software Engineering Intern, Decision Intelligence - Fall 2026` / NVIDIA / Santa Clara, CA / `4417503978`;
  (6) `Frontend Software Engineer Intern (Ads Measurement Signal and Privacy) - 2026 Summer (BS/MS)` / TikTok / San Jose, CA / `4359022183`.
  Term (saved-search phrase) = `software engineer intern`.
- **`.env.example`** — a "Gmail LinkedIn-alert parser (local `discover_gmail` worker)" block documenting
  `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` (+ the optional overrides) and how to mint an App Password.
- **`README.md`** — a "Gmail LinkedIn alerts (local, operator-run)" section (setup + run).

### Obviousness without confusion (naming)
- `source = "gmail:linkedin-alert"` — a stable, lowercase, colon-namespaced token like its siblings
  (`jobspy:indeed`, `github:simplify`). Self-describing in the triage `source` column, and **safe**: it is
  non-ATS, so `merge.py::_is_ats` correctly treats it as lower-primacy than a real ATS source. It must
  stay a token (not free-form) because merge primacy and source filters key off the prefix.
- The human "which saved search" signal rides in **`term`** (the header phrase), which already surfaces in
  triage — display metadata, not control logic.
- The email **subject is deliberately NOT used** as an identifier (LinkedIn sets it to the first job's
  title+company — misleading).

### Data flow
IMAP (`FROM jobalerts-noreply@linkedin.com SINCE now-7d`) → raw message bytes → `parse_linkedin_alert`
(text/plain → `JobRecord`s, `source="gmail:linkedin-alert"`, `description=None`, `term=[phrase]`) →
`ingest_records` (dedupe by `company|title` + canonicalize + upsert). Rows enter the same `jobs` table as
every other source, so routing / the triage table / the recency sort work on them unchanged.

### Enrichment interplay (link-only rows)
LinkedIn is not an enrichable ATS host, so these rows stay `description=None` unless the **same job**
(same `company|title` dedupe key) later arrives from an ATS/GitHub source — then `merge_records` fills the
description, the ATS row becomes primary, and the LinkedIn URL is preserved in `alt_urls`. Meanwhile the
rows are **routable on title** via the existing title-triage path (`route` classifies on title/slug when
no JD is present). Net: LinkedIn-curated postings enter the feed with title+company+location+term,
deduped against every other source — exactly the PRD's "link-only, enriched later" model. Scraping
LinkedIn job pages for JDs is out of scope (ban risk; that is JobSpy's channel).

### Idempotency
Job-level dedupe is by `company|title`, so re-reading the same emails just re-upserts harmlessly. The
worker therefore re-scans a rolling `SINCE now-7d` window each run — **no seen/message-id bookkeeping**,
matching the project's idempotent-upsert philosophy.

### Error handling / robustness
- **Fail-soft per message** — one unparseable email is caught, logged, skipped; the run completes.
- **Defensive parse** — blocks missing a jobid/title/company are skipped; the header/social/footer lines
  are filtered; a format change degrades to "fewer rows," never a crash.
- **Bounded** — one IMAP window per run; `ingest_records`' age cutoff still applies (null `posted_at` kept).
- **Credentials** — missing env → clear `RuntimeError` before any network call. App Password stays in
  local `.env`, never in CI (an App Password grants full IMAP inbox access → keep it off GitHub).
- **Local-only** — no `.github/workflows` change; runs on the operator's machine + in the nightly scheduler.

### Testing (matches repo TDD; no CI change; all stdlib so CI runs it)
- **Unit — `tests/test_gmail_parse.py`:** `parse_linkedin_alert(fixture_bytes)` → asserts the exact **6**
  jobs from the real sample: titles/companies/locations, reconstructed clean `jobs/view/<id>` URLs (no
  `/comm/`, no query), `external_id`, `source == "gmail:linkedin-alert"`, `description is None`,
  `term == ["software engineer intern"]`, `dedupe_key == make_dedupe_key(company, title)`. Plus: a block
  missing a title is skipped; an email with no job cards → `[]`; a non-multipart / junk email → `[]`.
- **Integration — `tests/test_gmail_discover.py`** (pytest-postgresql `conn` + `apply_migrations`):
  `discover_gmail_alerts` with a **fake fetch** returning `[fixture_bytes]` → rows land in `jobs` (query
  the DB), `source`/`term` set, dedupe holds (same fixture twice → 6 rows, not 12), and a fetch returning
  one good + one junk message still ingests the good one (fail-soft).
- **No live IMAP in tests** — the fetch boundary is injected; `_imap_fetch` itself (stdlib `imaplib`) is
  the thin untested side-effect, validated by the operator's first real run.

## Out of scope
Scraping LinkedIn job pages for JDs (ban risk; JobSpy's channel); parsing the HTML MIME part (plain-text
is stabler); Gmail API / OAuth (rejected — restricted-scope wall); storing `savedSearchId` (opaque;
`term` is the human signal); a `gmail` dependency extra (everything is stdlib); running in CI (credential
stays local). Multiple-inbox / non-LinkedIn alert sources are future work.

## Execution
Spec + plan committed to `main` (as with prior features); implementation in an isolated git worktree via
subagent-driven TDD, two-stage review per task, merge to `main`, push (gh `vaibhavw30`).
