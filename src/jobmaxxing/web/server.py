"""Flask triage server — localhost-only, no auth.

Flask is imported lazily (inside create_app) so this module can be imported
without flask installed. The non-server tests rely on this.
"""

import logging
import os

logger = logging.getLogger(__name__)

# Status dropdown options; "undecided" and "all" are pseudo-filters.
_STATUS_OPTIONS = ["undecided", "all", "new", "routed", "approved_for_tailoring",
                   "tailored", "reviewed", "applied", "rejected"]

# Sortable columns: (sort_key, header_label, default_direction).
_SORT_HEADERS = [("company", "Company", "asc"), ("type", "Resume", "asc"),
                 ("conf", "Conf", "desc"), ("posted", "Posted", "desc")]

# ---------------------------------------------------------------------------
# INDEX_HTML — inline template rendered with render_template_string
# ---------------------------------------------------------------------------

INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job Triage</title>
<style>
  body { font-family: system-ui, sans-serif; font-size: 13px; margin: 0; background: #f5f5f5; }
  table { border-collapse: collapse; width: 100%; background: #fff; }
  thead th { position: sticky; top: 0; background: #1a1a2e; color: #fff; padding: 8px 10px;
             text-align: left; z-index: 10; white-space: nowrap; }
  tbody tr:nth-child(odd) { background: #fafafa; }
  tbody tr:nth-child(even) { background: #f0f4ff; }
  td { padding: 6px 10px; vertical-align: top; border-bottom: 1px solid #e0e0e0; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px;
           font-weight: 600; white-space: nowrap; }
  .badge-routed { background: #dbeafe; color: #1d4ed8; }
  .badge-new { background: #e0f2fe; color: #0369a1; }
  .badge-approved_for_tailoring { background: #dcfce7; color: #15803d; }
  .badge-rejected { background: #fee2e2; color: #b91c1c; }
  .badge-applied { background: #fef9c3; color: #92400e; }
  .badge-tailored { background: #f3e8ff; color: #7e22ce; }
  .badge-other { background: #e5e7eb; color: #374151; }
  details summary { cursor: pointer; color: #6366f1; font-size: 11px; }
  details[open] summary { margin-bottom: 4px; }
  details pre { white-space: pre-wrap; max-height: 200px; overflow-y: auto;
                background: #f8f8f8; padding: 6px; font-size: 11px; }
  a.posting-link { color: #4f46e5; font-weight: 600; text-decoration: none; font-size: 11px; }
  a.posting-link:hover { text-decoration: underline; }
  .controls { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
  button { cursor: pointer; border: none; border-radius: 4px; padding: 4px 10px; font-size: 12px;
           font-weight: 600; }
  .btn-yes { background: #16a34a; color: #fff; }
  .btn-no  { background: #dc2626; color: #fff; }
  .btn-applied { background: #d97706; color: #fff; }
  .btn-reset { background: #6b7280; color: #fff; font-size: 11px; padding: 3px 7px; }
  button:hover { opacity: 0.85; }
  .company { font-weight: 700; }
  .title { color: #374151; }
  .bar { padding: 8px 10px; background: #1a1a2e; color: #fff; display: flex; gap: 16px;
         align-items: center; flex-wrap: wrap; font-size: 12px; }
  .bar select { font-size: 12px; padding: 2px 4px; }
  .bar .count { margin-left: auto; color: #c7d2fe; }
  thead th a { color: #fff; text-decoration: none; }
  thead th a:hover { text-decoration: underline; }
  .conf { text-align: right; color: #6b7280; font-variant-numeric: tabular-nums; }
  .posted { white-space: nowrap; color: #374151; }
</style>
</head>
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
    <label>Term
      <select name="term" onchange="this.form.submit()">
        <option value="" {{ 'selected' if term_sel == '' else '' }}>all</option>
        <option value="__untagged__" {{ 'selected' if term_sel == '__untagged__' else '' }}>untagged</option>
        {% for t in term_options %}
        <option value="{{ t }}" {{ 'selected' if t == term_sel else '' }}>{{ t }}</option>
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
  <tbody>
  {% for row in rows %}
  <tr id="row-{{ row.id }}">
    <td class="company">{{ row.company }}</td>
    <td class="title">{{ row.title }}</td>
    <td>{{ row.resume_type or '' }}</td>
    <td>{{ row.term | join(', ') if row.term else '—' }}</td>
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
  <tr><td colspan="10" style="text-align:center;padding:20px;color:#9ca3af;">No jobs to triage.</td></tr>
  {% endfor %}
  </tbody>
</table>
<script>
function setBadge(jobId, status) {
  var el = document.getElementById('badge-' + jobId);
  if (!el) return;
  // clear old badge classes
  el.className = el.className.replace(/badge-\\S+/g, '').trim();
  el.classList.add('badge');
  el.classList.add('badge-' + status);
  el.textContent = status;
}

async function decide(jobId, payload, event) {
  payload.job_id = jobId;
  var btn = event.currentTarget;
  btn.disabled = true;
  try {
    var resp = await fetch('/decide', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    if (resp.ok) {
      var data = await resp.json();
      if (data.changed) { setBadge(jobId, data.status); }
    } else {
      var err = await resp.text();
      alert('Error ' + resp.status + ': ' + err);
    }
  } catch (e) {
    alert('Network error: ' + e);
  } finally {
    btn.disabled = false;
  }
}

async function doReset(jobId, event) {
  var btn = event.currentTarget;
  btn.disabled = true;
  try {
    var resp = await fetch('/reset', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({job_id: jobId})
    });
    if (resp.ok) {
      var data = await resp.json();
      if (data.changed) { setBadge(jobId, data.status); }
    } else {
      var err = await resp.text();
      alert('Error ' + resp.status + ': ' + err);
    }
  } catch (e) {
    alert('Network error: ' + e);
  } finally {
    btn.disabled = false;
  }
}
</script>
</body>
</html>
"""


def _build_headers(active_sort, active_dir, status_sel, resume_type_sel, term_sel=""):
    """Ordered <th> descriptors. Sortable columns get an href that toggles direction and
    preserves active filters; non-sortable columns get href=None. Order matches the row cells."""
    from urllib.parse import urlencode
    base = {}
    if status_sel:
        base["status"] = status_sel
    if resume_type_sel:
        base["resume_type"] = resume_type_sel
    if term_sel:
        base["term"] = term_sel
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
    order = [("company", None), (None, "Title"), ("type", None), (None, "Term"),
             ("conf", None), ("posted", None), (None, "Status"), (None, "JD"),
             (None, "Link"), (None, "Actions")]
    headers = []
    for key, plain_label in order:
        if key:
            h = sortable[key]
            headers.append({"label": h["label"], "href": h["href"], "arrow": h["arrow"]})
        else:
            headers.append({"label": plain_label, "href": None, "arrow": ""})
    return headers


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(conn_factory):
    """Build and return the Flask app.

    Flask is imported here (lazily) so the module-level import of server.py
    does NOT require flask to be installed.
    """
    from flask import Flask, jsonify, render_template_string, request

    from .triage import apply_decision, count_triage, fetch_triage_rows, reset_to_routed

    app = Flask(__name__)

    _ALLOWED_HOSTS = {"127.0.0.1", "localhost"}

    @app.before_request
    def _host_guard():
        host = request.host.split(":")[0]
        if host not in _ALLOWED_HOSTS:
            return ("forbidden host", 403)

    @app.get("/favicon.ico")
    def favicon():
        return ("", 204)

    @app.get("/")
    def index():
        status_arg = request.args.get("status") or "undecided"
        resume_type_arg = request.args.get("resume_type") or None
        sort_arg = request.args.get("sort") or None
        dir_arg = request.args.get("dir") or None
        term_arg = request.args.get("term") or None

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
                                     resume_type=resume_type_arg, term=term_arg,
                                     sort=sort_arg, direction=dir_arg)
            total = count_triage(conn, status=status, statuses=statuses,
                                 resume_type=resume_type_arg, term=term_arg)
            cats = [r[0] for r in conn.execute(
                "select distinct resume_type from jobs where resume_type is not null order by 1"
            ).fetchall()]
            term_opts = [r[0] for r in conn.execute(
                "select distinct unnest(term) as t from jobs "
                "where term is not null and cardinality(term) > 0 order by t"
            ).fetchall()]

        for row in rows:
            row["id"] = str(row["id"])

        headers = _build_headers(sort_arg, dir_arg, status_arg, resume_type_arg or "",
                                 term_arg or "")
        return render_template_string(
            INDEX_HTML, rows=rows, headers=headers, total=total, shown=len(rows),
            status_options=_STATUS_OPTIONS, status_sel=status_arg,
            categories=cats, resume_type_sel=(resume_type_arg or ""),
            active_sort=(sort_arg or ""), active_dir=(dir_arg or ""),
            term_options=term_opts, term_sel=(term_arg or ""),
        )

    @app.post("/decide")
    def decide():
        if not request.is_json:
            return ("unsupported media type: expected application/json", 415)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return ("malformed or empty JSON body", 400)
        job_id = body.get("job_id")
        if not job_id:
            return ("missing job_id", 400)
        interested = body.get("interested")
        applied = body.get("applied")
        try:
            with conn_factory() as conn:
                result = apply_decision(conn, job_id, interested=interested, applied=applied)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify(result)

    @app.post("/reset")
    def reset():
        if not request.is_json:
            return ("unsupported media type: expected application/json", 415)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return ("malformed or empty JSON body", 400)
        job_id = body.get("job_id")
        if not job_id:
            return ("missing job_id", 400)
        with conn_factory() as conn:
            result = reset_to_routed(conn, job_id)
        return jsonify(result)

    return app


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    import psycopg

    from ..config import load_settings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    conn_factory = lambda: psycopg.connect(settings.database_url)
    port = int(os.environ.get("WEB_PORT", "8765"))
    app = create_app(conn_factory)
    logger.info("Starting triage server on http://127.0.0.1:%d", port)
    app.run(host="127.0.0.1", port=port, threaded=False)
