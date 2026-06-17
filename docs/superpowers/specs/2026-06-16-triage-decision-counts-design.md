# Triage decision counts in header — design

## Context
The triage UI persists operator decisions durably (`POST /decide` → `apply_decision` writes `status`
in a `conn.transaction()`; nothing overwrites it; it survives restarts). However, decided jobs leave
the default "undecided" view (`status in ('new','routed')`) and appear to vanish, making the operator
worry their actions were lost.

## Goal
Show **per-status decision counts** in the triage header bar so the operator can see their durable
decisions accumulating at a glance — e.g. `"Undecided 909 · Interested 12 · Applied 3 · Rejected 40"`.

## Design

### 1. `triage.decision_counts(conn, *, in_window_labels=()) → dict[str, int]`
A new query in `src/jobmaxxing/web/triage.py` that counts all visible-triageable rows grouped by
`status`. "Visible" uses the same predicate as `fetch_triage_rows`:
```sql
select status, count(*) from jobs
where resume_type is not null
  and not (<off_window_sql(in_window_labels)>)
group by status
```
- Reuses `from ..normalize import off_window_sql` (already imported) so counts match what the
  operator actually sees in the table — no divergence between count and rows.
- Returns e.g. `{"routed": 900, "new": 9, "approved_for_tailoring": 12, "applied": 3, "rejected": 40}`.
  Missing statuses are absent from the dict (not zero-filled), except the template renders zero-counts
  as absent.

### 2. Render in the header (`server.py` `index()` + `INDEX_HTML`)
In the `index()` route (already has `in_window` from `in_window_term_labels(...)`):
```python
dcounts = decision_counts(conn, in_window_labels=in_window)
```
Then pass `dcounts` to `render_template_string`. The template renders a compact summary in `.bar`:
```html
<span class="count-summary">
  Undecided {{ (dcounts.get('new', 0) + dcounts.get('routed', 0)) }}
  {% if dcounts.get('approved_for_tailoring', 0) %} · Interested {{ dcounts.get('approved_for_tailoring') }}{% endif %}
  ...
</span>
```

**Friendly label mapping** (status → label):
- `new` + `routed` → **Undecided** (always shown)
- `approved_for_tailoring` → **Interested**
- `tailored` → **Tailored**
- `reviewed` → **Reviewed**
- `applied` → **Applied**
- `rejected` → **Rejected**

Zero-count statuses are omitted except Undecided (always shown so the operator knows the backlog
even at zero).

### 3. CSS
Reuse `.bar .count` styling for the new summary span (same color `#c7d2fe`), or add a `.count-summary`
sibling. Kept inline in `.bar` so it wraps naturally on narrow screens.

## Components & data flow
- `src/jobmaxxing/web/triage.py` — new `decision_counts(conn, *, in_window_labels=()) → dict[str,int]`
- `src/jobmaxxing/web/server.py` — call `decision_counts` in `index()`, pass `dcounts` to template
- `src/jobmaxxing/web/server.py` — `INDEX_HTML` `.bar` renders the friendly summary span

## Testing (TDD)
- `tests/test_web_triage.py`: `test_decision_counts_groups_by_status` — seed rows with statuses
  `routed`, `approved_for_tailoring`, `applied`, `rejected`, plus one off-window row; assert
  `decision_counts(conn, in_window_labels=["Summer 2026"])` returns the right per-status dict and
  EXCLUDES the off-window row.
- `tests/test_web_server.py`: `test_header_shows_decision_counts` — seed an `approved_for_tailoring`
  row and an `applied` row (in-window term), GET `/`, assert the rendered HTML contains `Interested`
  and the correct count, and `Applied` with its count.

## Out of scope
- No change to the decision persistence logic (already durable).
- No per-filter (status/category/term) breakdown — always totals across all visible jobs.
- No live (JS) update of counts after a decision click — operator refreshes to see updated counts.

## Execution
Isolated worktree (`worktree-agent-aae235ec9bc4bf17c`); strict TDD; no merge to main.
