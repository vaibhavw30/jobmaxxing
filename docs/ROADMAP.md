# Roadmap & Path Forward

A single reference for where the internship-recruiting pipeline stands and what's next. For the *why*
and the full architecture, see [`PRD.md`](PRD.md) and
[`TECHNICAL_IMPLEMENTATION_PLAN.md`](TECHNICAL_IMPLEMENTATION_PLAN.md). Every feature below also has a
detailed spec + plan under `docs/superpowers/` (indexed at the bottom).

_Last updated: 2026-07-08._

---

## 1. Where things stand

| Area | State | Notes |
| --- | --- | --- |
| **Phase 1 — Core feed** (GitHub lists + Greenhouse/Lever/Ashby ATS → deduped `jobs` table) | ✅ Live | CI pollers every 3h |
| **Phase 2 — Routing** (deterministic rules + bounded LLM tiebreak → 8 resume types) | ✅ Live | LLM routing throttled to ~every 4 days (cost) |
| **Phase 3 — Tailoring** (two-pass loop + 5-axis scorer + one-page guard) | ✅ **Validated end-to-end** | Real `pdflatex` compile + composite delta confirmed (2026-07-08). ⚠️ Blocked on real content: base résumés are still scaffolds |
| **Phase 4 — Interface** (MCP server + local web triage table) | ✅ Live | Web triage superseded the Google-Sheets sync |
| **Phase 5a — JD enrichment** (clean-API ATS + headless Workday) | ✅ Live | Workday enrichment is a local worker |
| **JobSpy discovery** (Indeed/LinkedIn via `python-jobspy`) | ✅ Merged | ⚠️ Operator hasn't run the first live scrape yet |
| **Gmail LinkedIn-alert parser** (IMAP, saved-search alerts) | ✅ Merged | ⚠️ Operator hasn't run the first live IMAP fetch yet |
| **Local nightly scheduling** ("production cron", launchd) | ✅ Merged | ⚠️ Operator hasn't installed the LaunchAgent yet |
| **Phase 6 — Form-fill assist** (human-gated) | 🔲 Not designed | Optional, last phase; see §4 |

**One-line status:** every phase through 5 is built and merged; Phase 3 tailoring has now actually been
proven to work end-to-end (not just unit-tested). What's left is operator content/setup (§2) and the
optional, undesigned Phase 6.

---

## 2. Operator action items (things only you can do)

These are manual, one-time, and independent of further coding:

1. **Stand up real base résumés — the one true blocker on Phase 3.** Tailoring was validated end-to-end
   on 2026-07-08 against a hand-written résumé (real 1-page PDF, composite delta +2.7), but the 8 shipped
   `resume_store/base/{type}/main.tex` are still degenerate scaffolds — the model *refuses* to tailor an
   unfilled template (it emits prose like "this is an unfilled template" instead of LaTeX, so nothing
   compiles). Replace each with your real résumé content, either at `RESUME_STORE_DIR` (local) or
   `s3://<bucket>/base/{type}/main.tex` + `S3_BUCKET` (production). Until this is done, `tailor_job` will
   not produce a usable PDF for any real job.
2. **Install the nightly scheduler** — `cp scripts/com.jobmaxxing.nightly.plist ~/Library/LaunchAgents/`,
   edit the paths, `launchctl bootstrap gui/$(id -u) …`. Runs the residential-IP workers at 12am +
   notifies. (See the README "Nightly scheduling" section.)
3. **Run the first JobSpy scrape** — `uv sync --extra discovery && uv run python -m jobmaxxing.discover_jobspy`
   on your home IP; confirm `jobspy:*` rows land in triage with working links.
4. **Run the first Gmail alert fetch** — create LinkedIn saved searches (one per role) with daily email
   alerts, mint a Gmail App Password, set `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` in `.env`, then
   `uv run python -m jobmaxxing.discover_gmail`; confirm `gmail:linkedin-alert` rows land in triage.
5. **Ongoing tuning** — watch the rules-vs-LLM routing ratio (`config/routing.yaml`) and the per-type
   keyword dictionaries + weights (`rubrics/{type}.json`) against real tailored jobs.

---

## 3. Recently shipped: Gmail LinkedIn-alert parser + Phase 3 hardening

**Gmail parser (merged 2026-07-07, `origin/main` @ `f91cd5d`).** Full design:
[`docs/superpowers/specs/2026-07-07-gmail-alert-parser-design.md`](superpowers/specs/2026-07-07-gmail-alert-parser-design.md).
A local, operator-run worker (`python -m jobmaxxing.discover_gmail`) reads LinkedIn saved-search
**job-alert emails** over IMAP (Gmail **App Password**, not OAuth/the Gmail API — sidesteps the
sensitive-scope wall that killed the earlier Sheets integration) and ingests each listed posting as a
link-only row: `source="gmail:linkedin-alert"`, clean `linkedin.com/jobs/view/<id>` URL (tracking
dropped), `term=[saved-search phrase]`, `description=None` (routable on title; gets a real JD later if
the same job arrives from an ATS/GitHub source). All stdlib (`imaplib`+`email`+`re`) — no new dependency.
Runs first in the nightly batch (`WORKER_NAMES[0]`).

**Phase 3 five-axis scorer + fence/prose fixes (merged 2026-07-08, `origin/main` @ `41d996e`).** Full
design: [`docs/superpowers/specs/2026-07-08-five-axis-scorer-design.md`](superpowers/specs/2026-07-08-five-axis-scorer-design.md).
Implements the PRD §7 weighted composite (deterministic keyword coverage + 4 LLM-graded axes at
temperature-0 + a rubric-weighted composite, additive/backward-compatible with the old score dict) — AND
fixes two P0 bugs a first-ever real-`pdflatex` validation exposed: a leaked markdown code fence, and the
tailoring model intermittently prepending prose before `\documentclass`. Both fixed by extracting the
`\documentclass…\end{document}` span out of the model's raw response. **Validated 3/3 on a real résumé:**
a compiling one-page PDF with a reproducible composite delta of ~+2.7. This is the first time the
tailoring pipeline has been proven to work end to end, not just unit-tested.

**Both built the same way:** brainstorming → approved spec → `writing-plans` → subagent-driven TDD in an
isolated worktree, per-task spec+quality review, a whole-branch review, merge, push.

---

## 4. Later: Phase 6 — Form-fill assist (optional, not designed)

Human-gated auto-fill of application forms for **operator-approved** jobs only — never for discovery or
decision. This is an explicit v1 non-goal (the human gate is a hard architectural boundary) and has **no
spec yet**. If pursued, it would need its own brainstorming pass covering: which ATS form types to
support, how far to automate before the human submits, and how to keep it strictly downstream of approval.

---

## 5. Cross-cutting: the local-worker fleet

Five workers run on the operator's residential machine (never CI), because they need a home IP and/or
local credentials. The nightly launchd scheduler chains them at 12am, in this order:

| Worker | Why local | Extra |
| --- | --- | --- |
| `discover_gmail` | App Password stays off CI | — (stdlib) |
| `discover_jobspy` | job boards 429 datacenter IPs | `discovery` |
| `enrich_workday` | Cloudflare gentler on home IPs | `headless` |
| `recover_jd` | DuckDuckGo rate-limits datacenter IPs | — |
| `verify_url` | same | — |

Everything else — pollers, ATS enrichment, rules routing, LLM routing, the nightly digest email — runs in
CI on schedule. Tailoring is local + operator-gated (cost control), never automatic.

---

## 6. Document index

- **Product & architecture:** [`PRD.md`](PRD.md), [`TECHNICAL_IMPLEMENTATION_PLAN.md`](TECHNICAL_IMPLEMENTATION_PLAN.md)
- **Per-feature specs & plans:** `docs/superpowers/specs/` and `docs/superpowers/plans/` (dated by feature)
- **Most recent:** JobSpy discovery (`2026-07-02-jobspy-discovery*`), local nightly scheduling
  (`2026-07-07-local-scheduling*`), Gmail alert parser (`2026-07-07-gmail-alert-parser*`), the five-axis
  tailoring scorer (`2026-07-08-five-axis-scorer*`)
- **How to run everything:** `README.md`
