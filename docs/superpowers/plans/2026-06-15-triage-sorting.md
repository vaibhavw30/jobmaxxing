# Triage table sorting & filtering — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the local web triage table sort by posting recency (default), company, category, and routing confidence; filter by category and status; and show a "showing N of M" count.

**Architecture:** Two layers (unchanged boundary). `web/triage.py` (pure DB logic, no Flask) gains a whitelist-driven ORDER BY, a new "recent + relevant" default order, a `count_triage`, a shared WHERE builder, and a `route_confidence` display column. `web/server.py` (thin Flask) wires `sort`/`dir`/`status`/`resume_type` query params into the rows + count, and the inline `INDEX_HTML` gets clickable sort headers, a Posted + Conf column, two filter dropdowns, and the count indicator.

**Tech Stack:** Python 3.12, psycopg3, Flask (opt-in `web` extra), pytest-postgresql. Spec: `docs/superpowers/specs/2026-06-15-triage-sorting-design.md`.

**Run tests** (from the worktree, Postgres binary on PATH):
`export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run --extra web pytest <paths> -q`

---

## Task 1: triage.py foundation — shared WHERE, count, display column, raised cap

**Files:**
- Modify: `src/jobmaxxing/web/triage.py`
- Test: `tests/test_web_triage.py`

This task does NOT change ordering yet (Task 2). It extracts a shared WHERE builder, adds `count_triage`, adds `route_confidence` to the selected/display columns, raises the cap to 500, and upgrades the test `_insert` helper to seed `posted_at`/`route_confidence`/`route_method`.

- [ ] **Step 1: Upgrade the `_insert` helper in `tests/test_web_triage.py`** to support arbitrary optional columns. Replace the existing `_insert` function (currently lines ~28-40) with:

```python
def _insert(conn, *, dedupe_key, resume_type="swe", status="routed", description="<p>jd</p>",
            company="Acme", title="SWE Intern", scraped_at=None, posted_at=None,
            route_confidence=None, route_method=None):
    cols = ["dedupe_key", "source", "company", "title", "url", "description", "resume_type", "status"]
    vals = [dedupe_key, "github:simplify", company, title, f"https://x/{dedupe_key}",
            description, resume_type, status]
    for name, value in (("scraped_at", scraped_at), ("posted_at", posted_at),
                        ("route_confidence", route_confidence), ("route_method", route_method)):
        if value is not None:
            cols.append(name)
            vals.append(value)
    placeholders = ", ".join(["%s"] * len(vals))
    conn.execute(f"insert into jobs ({', '.join(cols)}) values ({placeholders})", vals)
    conn.commit()
    return str(conn.execute("select id from jobs where dedupe_key=%s", (dedupe_key,)).fetchone()[0])
```

Run the existing suite to confirm nothing broke (the new params default to None → identical behavior):
Run: `uv run --extra web pytest tests/test_web_triage.py -q`
Expected: PASS (same count as before).

- [ ] **Step 2: Write failing tests** for `count_triage` and the `route_confidence` display column. Add to `tests/test_web_triage.py`:

```python
from jobmaxxing.web.triage import count_triage  # add to existing import line

def test_count_triage_matches_filters_ignoring_limit(conn):
    for i in range(5):
        _insert(conn, dedupe_key=f"c|swe|{i}", resume_type="swe", status="routed")
    _insert(conn, dedupe_key="c|swe|rej", resume_type="swe", status="rejected")
    _insert(conn, dedupe_key="c|mle", resume_type="mle", status="routed")
    # count honors the same filters as fetch, ignores limit
    assert count_triage(conn) == 7                                   # all routed
    assert count_triage(conn, statuses=("new", "routed")) == 6      # undecided
    assert count_triage(conn, resume_type="swe") == 6
    assert count_triage(conn, statuses=("routed",), resume_type="mle") == 1

def test_fetch_includes_route_confidence(conn):
    _insert(conn, dedupe_key="rc|1", route_confidence=0.83)
    rows = fetch_triage_rows(conn)
    assert "route_confidence" in rows[0]
    assert abs(rows[0]["route_confidence"] - 0.83) < 1e-6
```

Run: `uv run --extra web pytest tests/test_web_triage.py::test_count_triage_matches_filters_ignoring_limit tests/test_web_triage.py::test_fetch_includes_route_confidence -q`
Expected: FAIL (`cannot import name 'count_triage'` / missing key).

- [ ] **Step 3: Refactor `src/jobmaxxing/web/triage.py`** — extract the WHERE builder, add the display column + count, raise the cap. Replace the module top (lines 1-57, through the end of `fetch_triage_rows`) with:

```python
"""Triage DB layer — fetch routed jobs + apply operator decisions.

No Flask import. Takes a live psycopg conn as a parameter.
"""

from ..funnel import TRIAGE_COLUMNS, decision_to_status, plain_text

# Columns rendered by the web table: the canonical funnel set plus route_confidence
# (a display/relevance signal not part of the Sheets-facing TRIAGE_COLUMNS).
_DISPLAY_COLS = (*TRIAGE_COLUMNS, "route_confidence")

DEFAULT_LIMIT = 500
MAX_LIMIT = 500


def _build_where(status, statuses, resume_type):
    """Build the shared WHERE clause + params for fetch/count. Routed jobs only."""
    clauses = ["resume_type is not null"]
    params: list = []
    statuses_list = list(statuses) if statuses is not None else None
    if statuses is not None and not statuses_list:
        raise ValueError("statuses must be non-empty when provided")
    if statuses_list:
        placeholders = ", ".join(["%s"] * len(statuses_list))
        clauses.append(f"status in ({placeholders})")
        params.extend(statuses_list)
    elif status is not None:
        clauses.append("status = %s")
        params.append(status)
    if resume_type is not None:
        clauses.append("resume_type = %s")
        params.append(resume_type)
    return " and ".join(clauses), params


def fetch_triage_rows(conn, *, status=None, statuses=None, resume_type=None,
                      sort=None, direction=None, limit=DEFAULT_LIMIT) -> list[dict]:
    """Return routed jobs (resume_type IS NOT NULL) as a list of column-keyed dicts.

    Filters: status= (single), statuses= (IN list; precedence over status=), resume_type=.
    Sorting: see _order_by (Task 2). description is returned as plain text.
    Capped at MAX_LIMIT rows.
    """
    where, params = _build_where(status, statuses, resume_type)
    order = _order_by(sort, direction)
    capped = max(1, min(int(limit), MAX_LIMIT))
    sql = f"select {', '.join(_DISPLAY_COLS)} from jobs where {where} {order} limit %s"
    rows = conn.execute(sql, params + [capped]).fetchall()

    result = []
    for row in rows:
        d = dict(zip(_DISPLAY_COLS, row))
        d["description"] = plain_text(d["description"])
        result.append(d)
    return result


def count_triage(conn, *, status=None, statuses=None, resume_type=None) -> int:
    """Total rows matching the same filters as fetch_triage_rows, ignoring sort/limit."""
    where, params = _build_where(status, statuses, resume_type)
    return conn.execute(f"select count(*) from jobs where {where}", params).fetchone()[0]
```

Then add a TEMPORARY `_order_by` so this task's code runs (Task 2 replaces it). Add immediately above `fetch_triage_rows`:

```python
def _order_by(sort, direction):  # replaced in Task 2
    return "order by scraped_at desc"
```

Leave `apply_decision` and `reset_to_routed` (current lines 60-97) unchanged.

- [ ] **Step 4: Run the tests** — new + existing.
Run: `uv run --extra web pytest tests/test_web_triage.py -q`
Expected: PASS (the two new tests + all existing; `test_fetch_limit_capped` still passes since 3 < 500 and limit=2 still caps to 2).

- [ ] **Step 5: Commit**
```bash
git add src/jobmaxxing/web/triage.py tests/test_web_triage.py
git commit -m "web(triage): shared WHERE builder, count_triage, route_confidence column, cap 500"
```

---

## Task 2: triage.py sorting — whitelist ORDER BY + "recent + relevant" default

**Files:**
- Modify: `src/jobmaxxing/web/triage.py`
- Test: `tests/test_web_triage.py`

- [ ] **Step 1: Update `test_fetch_orders_newest_first`** in `tests/test_web_triage.py` to assert posting-date order (the default now leads with `posted_at desc`). Find the existing `test_fetch_orders_newest_first` and replace its body with:

```python
def test_fetch_orders_newest_first(conn):
    """Default order leads with posted_at desc within one confidence tier."""
    from datetime import datetime, timezone
    old = _insert(conn, dedupe_key="o|old", posted_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                  route_confidence=0.9)
    new = _insert(conn, dedupe_key="o|new", posted_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                  route_confidence=0.9)
    ids = [str(r["id"]) for r in fetch_triage_rows(conn)]
    assert ids.index(new) < ids.index(old)
```

- [ ] **Step 2: Write failing tests** for the default demotion + each sort key. Add to `tests/test_web_triage.py`:

```python
def test_default_demotes_low_confidence_below_high(conn):
    """A RECENT low-confidence job ranks below an OLDER high-confidence one (tier beats recency)."""
    from datetime import datetime, timezone
    recent_low = _insert(conn, dedupe_key="d|recent_low",
                         posted_at=datetime(2026, 6, 10, tzinfo=timezone.utc), route_confidence=0.2)
    older_high = _insert(conn, dedupe_key="d|older_high",
                         posted_at=datetime(2026, 1, 1, tzinfo=timezone.utc), route_confidence=0.9)
    ids = [str(r["id"]) for r in fetch_triage_rows(conn)]
    assert ids.index(older_high) < ids.index(recent_low)

def test_default_null_confidence_treated_as_high(conn):
    """NULL route_confidence (e.g. manual) is treated as high-trust, not floated to the top spuriously."""
    from datetime import datetime, timezone
    null_conf = _insert(conn, dedupe_key="d|null", posted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                        route_confidence=None)
    recent_high = _insert(conn, dedupe_key="d|rh", posted_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                          route_confidence=0.9)
    ids = [str(r["id"]) for r in fetch_triage_rows(conn)]
    # both are in the high tier, so pure recency decides: recent_high first
    assert ids.index(recent_high) < ids.index(null_conf)

def test_sort_company_asc_and_desc(conn):
    a = _insert(conn, dedupe_key="s|c|a", company="Alpha")
    z = _insert(conn, dedupe_key="s|c|z", company="Zeta")
    asc = [str(r["id"]) for r in fetch_triage_rows(conn, sort="company", direction="asc")]
    assert asc.index(a) < asc.index(z)
    desc = [str(r["id"]) for r in fetch_triage_rows(conn, sort="company", direction="desc")]
    assert desc.index(z) < desc.index(a)

def test_sort_posted_is_pure_recency_ignoring_confidence(conn):
    """The 'posted' key sorts by posted_at only — a recent low-confidence job leads."""
    from datetime import datetime, timezone
    recent_low = _insert(conn, dedupe_key="s|p|rl",
                         posted_at=datetime(2026, 6, 10, tzinfo=timezone.utc), route_confidence=0.1)
    older_high = _insert(conn, dedupe_key="s|p|oh",
                         posted_at=datetime(2026, 1, 1, tzinfo=timezone.utc), route_confidence=0.9)
    ids = [str(r["id"]) for r in fetch_triage_rows(conn, sort="posted", direction="desc")]
    assert ids.index(recent_low) < ids.index(older_high)

def test_sort_type_groups_then_recency(conn):
    from datetime import datetime, timezone
    ai_new = _insert(conn, dedupe_key="s|t|ai_new", resume_type="ai",
                     posted_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    ai_old = _insert(conn, dedupe_key="s|t|ai_old", resume_type="ai",
                     posted_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    swe = _insert(conn, dedupe_key="s|t|swe", resume_type="swe",
                  posted_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    ids = [str(r["id"]) for r in fetch_triage_rows(conn, sort="type", direction="asc")]
    # ai grouped before swe; within ai, newest first
    assert ids.index(ai_new) < ids.index(ai_old) < ids.index(swe)

def test_sort_confidence_desc(conn):
    lo = _insert(conn, dedupe_key="s|conf|lo", route_confidence=0.2)
    hi = _insert(conn, dedupe_key="s|conf|hi", route_confidence=0.95)
    ids = [str(r["id"]) for r in fetch_triage_rows(conn, sort="conf", direction="desc")]
    assert ids.index(hi) < ids.index(lo)

def test_sort_unknown_key_falls_back_to_default(conn):
    """An unknown sort key is ignored (no error, no injection) and uses the default order."""
    from datetime import datetime, timezone
    old = _insert(conn, dedupe_key="s|u|old", posted_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                  route_confidence=0.9)
    new = _insert(conn, dedupe_key="s|u|new", posted_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                  route_confidence=0.9)
    ids = [str(r["id"]) for r in fetch_triage_rows(conn, sort="company); drop table jobs--", direction="x")]
    assert ids.index(new) < ids.index(old)  # default order, table intact
```

Run: `uv run --extra web pytest tests/test_web_triage.py -q`
Expected: FAIL (current `_order_by` returns `scraped_at desc`; sort params ignored).

- [ ] **Step 3: Replace the temporary `_order_by`** in `src/jobmaxxing/web/triage.py` with the real whitelist implementation + constants. Replace the `DEFAULT_LIMIT`/`MAX_LIMIT` constant block AND the temporary `_order_by` stub with:

```python
DEFAULT_LIMIT = 500
MAX_LIMIT = 500

# Jobs with route_confidence below this are demoted (second tier) in the default order.
# 0.4 matches the provisional title-only (route_method='llm_title') confidence cap.
RELEVANCE_FLOOR = 0.4

# Whitelist of clickable-header sort keys -> (sql_expression, default_direction, secondary).
# Expressions are FIXED strings (never user input) -> no SQL injection.
_SORTS = {
    "posted":  ("posted_at",       "desc", ""),
    "company": ("lower(company)",  "asc",  ""),
    "type":    ("resume_type",     "asc",  ", posted_at desc"),
    "conf":    ("route_confidence", "desc", ", posted_at desc"),
}


def _order_by(sort, direction):
    """Build an ORDER BY from the whitelist. Unknown sort -> the 'recent + relevant' default."""
    if sort in _SORTS:
        expr, default_dir, secondary = _SORTS[sort]
        d = direction if direction in ("asc", "desc") else default_dir
        return f"order by {expr} {d}{secondary}, id asc"
    # Default: high-confidence tier first, newest posting first within it.
    # RELEVANCE_FLOOR is a trusted constant, formatted as a literal (not user input).
    return (f"order by (coalesce(route_confidence, 1.0) < {RELEVANCE_FLOOR}) asc,"
            f" posted_at desc nulls last, id asc")
```

(Place `RELEVANCE_FLOOR` and `_SORTS` and `_order_by` ABOVE `fetch_triage_rows`. Remove the temporary stub from Task 1.)

- [ ] **Step 4: Run the tests.**
Run: `uv run --extra web pytest tests/test_web_triage.py -q`
Expected: PASS (all sort/default tests + existing).

- [ ] **Step 5: Commit**
```bash
git add src/jobmaxxing/web/triage.py tests/test_web_triage.py
git commit -m "web(triage): whitelist sort keys + recent+relevant default order"
```

---

## Task 3: server.py — sort headers, filters, Posted/Conf columns, count indicator

**Files:**
- Modify: `src/jobmaxxing/web/server.py`
- Test: `tests/test_web_server.py`

- [ ] **Step 1: Write failing server tests.** Add to `tests/test_web_server.py` (it already has the `conn`/`_insert`/`client` fixtures and `pytest.importorskip("flask")`). If the local `_insert` there lacks `posted_at`/`route_confidence`, copy the upgraded `_insert` from `tests/test_web_triage.py`. Add:

```python
def test_get_sort_company_orders_rows_in_html(client, conn):
    _insert(conn, dedupe_key="srv|zeta", company="ZetaCorp")
    _insert(conn, dedupe_key="srv|alpha", company="AlphaCorp")
    html = client.get("/?sort=company&dir=asc").get_data(as_text=True)
    assert html.index("AlphaCorp") < html.index("ZetaCorp")

def test_get_header_link_toggles_direction(client, conn):
    _insert(conn, dedupe_key="srv|h1", company="AlphaCorp")
    # When already sorted company asc, the Company header link should point to dir=desc
    html = client.get("/?sort=company&dir=asc").get_data(as_text=True)
    assert "sort=company&amp;dir=desc" in html or "sort=company&dir=desc" in html

def test_get_sort_links_preserve_filters(client, conn):
    _insert(conn, dedupe_key="srv|f1", resume_type="swe")
    html = client.get("/?resume_type=swe").get_data(as_text=True)
    # header sort links carry the active category filter
    assert "resume_type=swe" in html and "sort=" in html

def test_get_shows_count_indicator(client, conn):
    for i in range(3):
        _insert(conn, dedupe_key=f"srv|cnt|{i}")
    html = client.get("/").get_data(as_text=True)
    assert "of 3" in html  # "showing 3 of 3"

def test_get_status_all_includes_decided(client, conn):
    _insert(conn, dedupe_key="srv|all|routed", status="routed", company="RoutedCo")
    _insert(conn, dedupe_key="srv|all|rej", status="rejected", company="RejectedCo")
    html = client.get("/?status=all").get_data(as_text=True)
    assert "RoutedCo" in html and "RejectedCo" in html

def test_get_default_excludes_decided(client, conn):
    routed = _insert(conn, dedupe_key="srv|def|routed", status="routed", company="RoutedCo")
    rejected = _insert(conn, dedupe_key="srv|def|rej", status="rejected", company="RejectedCo")
    html = client.get("/").get_data(as_text=True)
    assert "RoutedCo" in html and "RejectedCo" not in html
```

(If a `test_get_default_excludes_decided` already exists from the prior feature, keep one copy.)

Run: `uv run --extra web pytest tests/test_web_server.py -q`
Expected: FAIL (no sort handling / count / status=all yet).

- [ ] **Step 2: Rewrite the `index()` route + add a header-link helper** in `src/jobmaxxing/web/server.py`. Add these module-level constants near the top (after `logger = ...`):

```python
# Status dropdown options (label shown == value sent); "undecided" and "all" are pseudo-filters.
_STATUS_OPTIONS = ["undecided", "all", "new", "routed", "approved_for_tailoring",
                   "tailored", "reviewed", "applied", "rejected"]

# Sortable columns: (sort_key, header_label, default_direction).
_SORT_HEADERS = [("company", "Company", "asc"), ("type", "Resume", "asc"),
                 ("conf", "Conf", "desc"), ("posted", "Posted", "desc")]
```

Add a helper (above `create_app`):

```python
def _build_headers(active_sort, active_dir, status_sel, resume_type_sel):
    """Return the ordered list of <th> descriptors for the table head.

    Sortable columns get an href that toggles direction and preserves the active
    filters; non-sortable columns get href=None. Order matches the row cells below.
    """
    from urllib.parse import urlencode
    base = {}
    if status_sel:
        base["status"] = status_sel
    if resume_type_sel:
        base["resume_type"] = resume_type_sel
    sortable = {}
    for key, label, default_dir in _SORT_HEADERS:
        if active_sort == key:
            new_dir = "asc" if active_dir == "desc" else "desc"
            arrow = " ↑" if active_dir == "asc" else " ↓"
        else:
            new_dir, arrow = default_dir, ""
        sortable[key] = {"label": label,
                         "href": "/?" + urlencode({**base, "sort": key, "dir": new_dir}),
                         "arrow": arrow}
    # Full header order (interleave sortable + plain). key=None => plain header.
    order = [("company", None), (None, "Title"), ("type", None), ("conf", None),
             ("posted", None), (None, "Status"), (None, "JD"), (None, "Link"), (None, "Actions")]
    headers = []
    for key, plain_label in order:
        if key:
            h = sortable[key]
            headers.append({"label": h["label"], "href": h["href"], "arrow": h["arrow"]})
        else:
            headers.append({"label": plain_label, "href": None, "arrow": ""})
    return headers
```

First, update the `create_app` triage import (current line ~177) from
`from .triage import apply_decision, fetch_triage_rows, reset_to_routed` to:
```python
    from .triage import apply_decision, count_triage, fetch_triage_rows, reset_to_routed
```

Then replace the entire `index()` function (current lines ~193-207) with:

```python
    @app.get("/")
    def index():
        status_arg = request.args.get("status") or "undecided"
        resume_type_arg = request.args.get("resume_type") or None
        sort_arg = request.args.get("sort") or None
        dir_arg = request.args.get("dir") or None

        status = None
        statuses = None
        if status_arg == "undecided":
            statuses = ("new", "routed")
        elif status_arg == "all":
            pass  # no status filter
        else:
            status = status_arg

        with conn_factory() as conn:
            rows = fetch_triage_rows(conn, status=status, statuses=statuses,
                                     resume_type=resume_type_arg, sort=sort_arg, direction=dir_arg)
            total = count_triage(conn, status=status, statuses=statuses, resume_type=resume_type_arg)
            cats = [r[0] for r in conn.execute(
                "select distinct resume_type from jobs where resume_type is not null order by 1"
            ).fetchall()]

        for row in rows:
            row["id"] = str(row["id"])

        headers = _build_headers(sort_arg, dir_arg, status_arg, resume_type_arg or "")
        return render_template_string(
            INDEX_HTML, rows=rows, headers=headers, total=total, shown=len(rows),
            status_options=_STATUS_OPTIONS, status_sel=status_arg,
            categories=cats, resume_type_sel=(resume_type_arg or ""),
            active_sort=(sort_arg or ""), active_dir=(dir_arg or ""),
        )
```

(Note: `fetch_triage_rows`/`count_triage` are now imported inside `index()`; remove the top-of-`create_app` `from .triage import apply_decision, fetch_triage_rows, reset_to_routed` line's `fetch_triage_rows` if it causes an unused-import — keep `apply_decision, reset_to_routed` there for the POST routes.)

- [ ] **Step 3: Update `INDEX_HTML`** — add a controls bar (count + two filter dropdowns), make the `<thead>` render from `headers`, and add the Posted + Conf cells. In `src/jobmaxxing/web/server.py`:

(a) Add to the `<style>` block (before `</style>`):
```css
  .bar { padding: 8px 10px; background: #1a1a2e; color: #fff; display: flex; gap: 16px;
         align-items: center; flex-wrap: wrap; font-size: 12px; }
  .bar select { font-size: 12px; padding: 2px 4px; }
  .bar .count { margin-left: auto; color: #c7d2fe; }
  thead th a { color: #fff; text-decoration: none; }
  thead th a:hover { text-decoration: underline; }
  .conf { text-align: right; color: #6b7280; font-variant-numeric: tabular-nums; }
  .posted { white-space: nowrap; color: #374151; }
```

(b) Replace the `<body>` opening + the static `<thead>...</thead>` (current lines ~57-69) with a controls bar + dynamic head:
```html
<body>
<div class="bar">
  <form method="get" style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:0;">
    <input type="hidden" name="sort" value="{{ active_sort }}">
    <input type="hidden" name="dir" value="{{ active_dir }}">
    <label>Status
      <select name="status" onchange="this.form.submit()">
        {% for s in status_options %}
        <option value="{{ s }}" {{ 'selected' if s == status_sel else '' }}>{{ s }}</option>
        {% endfor %}
      </select>
    </label>
    <label>Category
      <select name="resume_type" onchange="this.form.submit()">
        <option value="" {{ 'selected' if resume_type_sel == '' else '' }}>all</option>
        {% for c in categories %}
        <option value="{{ c }}" {{ 'selected' if c == resume_type_sel else '' }}>{{ c }}</option>
        {% endfor %}
      </select>
    </label>
  </form>
  <span class="count">showing {{ shown }} of {{ total }} matching{% if total > shown %} — narrow with a filter{% endif %}</span>
</div>
<table>
  <thead>
    <tr>
      {% for h in headers %}
      <th>{% if h.href %}<a href="{{ h.href }}">{{ h.label }}{{ h.arrow }}</a>{% else %}{{ h.label }}{% endif %}</th>
      {% endfor %}
    </tr>
  </thead>
```

(c) Replace the row cells (current lines ~72-96) so the cell order matches the header order (Company, Title, Resume, Conf, Posted, Status, JD, Link, Actions):
```html
  <tr id="row-{{ row.id }}">
    <td class="company">{{ row.company }}</td>
    <td class="title">{{ row.title }}</td>
    <td>{{ row.resume_type or '' }}</td>
    <td class="conf">{{ '%.2f'|format(row.route_confidence) if row.route_confidence is not none else '—' }}</td>
    <td class="posted">{{ row.posted_at.strftime('%Y-%m-%d') if row.posted_at else '—' }}</td>
    <td>
      <span class="badge badge-{{ row.status }}" id="badge-{{ row.id }}">{{ row.status }}</span>
    </td>
    <td>
      <details>
        <summary>JD</summary>
        <pre>{{ row.description }}</pre>
      </details>
    </td>
    <td>
      <a class="posting-link" href="{{ row.url }}" target="_blank" rel="noopener">Open posting</a>
    </td>
    <td>
      <div class="controls">
        <button class="btn-yes" onclick="decide('{{ row.id }}', {interested:'yes'}, event)">&#10003; Yes</button>
        <button class="btn-no"  onclick="decide('{{ row.id }}', {interested:'no'}, event)">&#10007; No</button>
        <button class="btn-applied" onclick="decide('{{ row.id }}', {applied:'true'}, event)">Applied</button>
        <button class="btn-reset" onclick="doReset('{{ row.id }}', event)">&#8634; reset</button>
      </div>
    </td>
  </tr>
  {% else %}
  <tr><td colspan="9" style="text-align:center;padding:20px;color:#9ca3af;">No jobs to triage.</td></tr>
  {% endfor %}
```

(Update the empty-state `colspan` from 7 to 9.)

- [ ] **Step 4: Run the server tests + the full web suite.**
Run: `uv run --extra web pytest tests/test_web_server.py tests/test_web_triage.py tests/test_funnel.py -q`
Expected: PASS (all new server tests + the triage/funnel suites).

- [ ] **Step 5: Commit**
```bash
git add src/jobmaxxing/web/server.py tests/test_web_server.py
git commit -m "web(server): sortable headers, status+category filters, Posted/Conf columns, count"
```

---

## Verification (end to end)

1. Full suite: `export PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" && uv run --extra web pytest -q` → all green.
2. Live read-only smoke against the real DB (no writes):
   `set -a && . .env && set +a && WEB_PORT=8799 uv run --extra web python -m jobmaxxing.web` (background), then `curl`:
   - `GET /` → 200; confirm the **first rows have recent `posted_at`** (top dates near today) and that a Posted column + Conf column render.
   - `GET /?sort=company&dir=asc` → companies start at 'A…'.
   - `GET /?resume_type=quant-trader` → only quant-trader rows; the count line shows the smaller M; sort links still carry `resume_type=quant-trader`.
   - `GET /?status=all` → includes decided rows.
   Then stop the server (`pkill -f jobmaxxing.web`).

## Risks & notes
- **SQL injection:** sort expressions come ONLY from the `_SORTS` whitelist; `direction` is constrained to `{"asc","desc"}`; `RELEVANCE_FLOOR` is a trusted code constant. Filter values are bound parameters. Unknown sort → silent default (covered by `test_sort_unknown_key_falls_back_to_default`, which also passes an injection string).
- **Cap = 500:** sorting by Company/Type across a >500-row category truncates; the "of M" indicator makes this visible and the category filter narrows it. Real pagination is out of scope.
- **`route_confidence` NULL:** `coalesce(..., 1.0)` keeps manual/unknown-confidence jobs in the high tier and avoids NULLS-FIRST floating them to the top.
- **funnel.py / ROUTED_JOBS_SQL unchanged:** `_DISPLAY_COLS` is local to triage.py; the dormant Sheets path keeps its own columns and `scraped_at desc` order.

## Execution
Isolated git worktree off `main`; subagent-driven TDD, one task per subagent; two-stage review (spec → quality) per task; merge to `main`; push (gh `vaibhavw30`).
