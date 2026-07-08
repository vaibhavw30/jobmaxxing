# Roadmap & Path Forward

A single reference for where the internship-recruiting pipeline stands and what's next. For the *why*
and the full architecture, see [`PRD.md`](PRD.md) and
[`TECHNICAL_IMPLEMENTATION_PLAN.md`](TECHNICAL_IMPLEMENTATION_PLAN.md). Every feature below also has a
detailed spec + plan under `docs/superpowers/` (indexed at the bottom).

_Last updated: 2026-07-07._

---

## 1. Where things stand

| Area | State | Notes |
| --- | --- | --- |
| **Phase 1 — Core feed** (GitHub lists + Greenhouse/Lever/Ashby ATS → deduped `jobs` table) | ✅ Live | CI pollers every 3h |
| **Phase 2 — Routing** (deterministic rules + bounded LLM tiebreak → 8 resume types) | ✅ Live | LLM routing throttled to ~every 4 days (cost) |
| **Phase 3 — Tailoring** (two-pass loop + deterministic scorer + one-page guard) | ✅ Engine done | ⚠️ Needs operator content: base résumés in S3 (`s3://<bucket>/base/{type}/`) |
| **Phase 4 — Interface** (MCP server + local web triage table) | ✅ Live | Web triage superseded the Google-Sheets sync |
| **Phase 5a — JD enrichment** (clean-API ATS + headless Workday) | ✅ Live | Workday enrichment is a local worker |
| **JobSpy discovery** (Indeed/LinkedIn via `python-jobspy`) | ✅ Merged | ⚠️ Operator hasn't run the first live scrape yet |
| **Local nightly scheduling** ("production cron", launchd) | ✅ Merged | ⚠️ Operator hasn't installed the LaunchAgent yet |
| **Phase 5 — Gmail LinkedIn-alert parser** | 📐 Designed, not built | Spec on `main`; see §3 |
| **Phase 6 — Form-fill assist** (human-gated) | 🔲 Not designed | Optional, last phase; see §4 |

**One-line status:** the reliable core + broader discovery + automation are all live or merged; the two
remaining build items are the Gmail parser (designed) and the optional form-fill (not designed).

---

## 2. Operator action items (things only you can do)

These are manual, one-time, and independent of further coding:

1. **Install the nightly scheduler** — `cp scripts/com.jobmaxxing.nightly.plist ~/Library/LaunchAgents/`,
   edit the paths, `launchctl bootstrap gui/$(id -u) …`. Runs the residential-IP workers at 12am +
   notifies. (See the README "Nightly scheduling" section.)
2. **Run the first JobSpy scrape** — `uv sync --extra discovery && uv run python -m jobmaxxing.discover_jobspy`
   on your home IP; confirm `jobspy:*` rows land in triage with working links.
3. **Stand up tailoring content** — upload base résumés to `s3://<bucket>/base/{type}/main.tex` and set
   `S3_BUCKET` + AWS creds, so Phase 3 can actually tailor. (Engine is ready; content is yours.)
4. **When the Gmail parser ships** — create LinkedIn saved searches (one per role) with daily email
   alerts, mint a Gmail App Password, set `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` in `.env`.
5. **Ongoing tuning** — watch the rules-vs-LLM routing ratio (`config/routing.yaml`) and the per-type
   keyword dictionaries (`rubrics/{type}.json`) against real postings.

---

## 3. Next build: Phase 5 — Gmail LinkedIn-alert parser

**Status: designed, spec committed, not yet implemented.** Full design:
[`docs/superpowers/specs/2026-07-07-gmail-alert-parser-design.md`](superpowers/specs/2026-07-07-gmail-alert-parser-design.md).

**What it is.** A local, operator-run worker (`python -m jobmaxxing.discover_gmail`) that reads your
LinkedIn saved-search **job-alert emails** over IMAP and ingests the listed postings into the `jobs`
table as link-only rows — the only LinkedIn channel the pipeline touches (no logged-in scraping).

**Key decisions (settled during brainstorming):**
- **Auth = Gmail App Password over IMAP**, not the Gmail API. Reading Gmail via API needs a *restricted*
  OAuth scope — the same wall (sensitive-scope block, 7-day Testing-mode token expiry) that killed the
  earlier Google-Sheets integration. Your account is a Family-Link account but in the *exception* bucket
  (App Passwords generator present, IMAP tab present), so a static `.env` App Password works with no OAuth,
  no expiry, no verification.
- **Parse the `text/plain` MIME part**, not the HTML — it's cleanly structured (Title / Company /
  Location / `View job: <url>` per card) and far more stable. The digest header even carries the
  saved-search phrase.
- **All stdlib** — `imaplib` (fetch) + `email` (MIME) + `re` (parse). **No new dependency.**

**Shape.** A pure `parse_linkedin_alert(raw_email) → list[JobRecord]` behind an injected `_imap_fetch`,
plus `discover_gmail_alerts(conn, *, fetch, now)` → `ingest_records`. Per posting it extracts
title/company/location, reconstructs a clean `https://www.linkedin.com/jobs/view/<id>` URL (dropping the
`/comm/` tracking redirect), sets `term = [saved-search phrase]`, and `description = None`.

**Identity in the system.** `source = "gmail:linkedin-alert"` — a stable, namespaced token like
`jobspy:indeed` (kept a token, not free-form, so `merge.py`'s ATS-primacy check and source filters stay
correct). The human "which alert" signal rides in `term`.

**Enrichment interplay.** Link-only rows are routable on title (title-triage) and get a real description
later *iff* the same `company|title` job arrives from an ATS/GitHub source (`merge_records` fills it, ATS
becomes primary, the LinkedIn URL survives in `alt_urls`). Idempotent: it re-scans a rolling 7-day IMAP
window each run; re-reading emails just re-upserts.

**Runs where.** A local worker (App Password stays off CI) added as a 5th entry in the nightly scheduler.

**Marginal value (worth knowing).** Since JobSpy already pulls LinkedIn-via-library, this parser's added
coverage is the saved-search postings JobSpy/GitHub/ATS miss — arriving as bare links (no JD). Useful but
additive; not a critical-path source.

**Build note.** The test fixture is a real alert email → **must be scrubbed** before committing to this
public repo (recipient address, "intended for…" footer, and per-recipient tracking tokens —
`midToken`/`otpToken`/`trackingId`/`savedSearchId`/`List-Unsubscribe`). The parser asserts only on public
fields (titles/companies/locations/job-ids/term).

**To build it:** brainstorming is done and the spec is approved — the next step is `writing-plans` → a
subagent-driven TDD implementation in a worktree (same flow as JobSpy discovery and the nightly scheduler).

---

## 4. Later: Phase 6 — Form-fill assist (optional, not designed)

Human-gated auto-fill of application forms for **operator-approved** jobs only — never for discovery or
decision. This is an explicit v1 non-goal (the human gate is a hard architectural boundary) and has **no
spec yet**. If pursued, it would need its own brainstorming pass covering: which ATS form types to
support, how far to automate before the human submits, and how to keep it strictly downstream of approval.

---

## 5. Cross-cutting: the local-worker fleet

Five workers run on the operator's residential machine (never CI), because they need a home IP and/or
local credentials. The nightly launchd scheduler chains them at 12am:

| Worker | Why local | Extra |
| --- | --- | --- |
| `discover_jobspy` | job boards 429 datacenter IPs | `discovery` |
| `enrich_workday` | Cloudflare gentler on home IPs | `headless` |
| `recover_jd` | DuckDuckGo rate-limits datacenter IPs | — |
| `verify_url` | same | — |
| `discover_gmail` *(planned)* | App Password stays off CI | — (stdlib) |

Everything else — pollers, ATS enrichment, rules routing, LLM routing, the nightly digest email — runs in
CI on schedule. Tailoring is local + operator-gated (cost control), never automatic.

---

## 6. Document index

- **Product & architecture:** [`PRD.md`](PRD.md), [`TECHNICAL_IMPLEMENTATION_PLAN.md`](TECHNICAL_IMPLEMENTATION_PLAN.md)
- **Per-feature specs & plans:** `docs/superpowers/specs/` and `docs/superpowers/plans/` (dated by feature)
- **Most recent:** JobSpy discovery (`2026-07-02-jobspy-discovery*`), local nightly scheduling
  (`2026-07-07-local-scheduling*`), Gmail alert parser (`2026-07-07-gmail-alert-parser-design.md` — spec only)
- **How to run everything:** `README.md`
