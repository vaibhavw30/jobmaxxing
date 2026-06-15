"""Flask triage server — localhost-only, no auth.

Flask is imported lazily (inside create_app) so this module can be imported
without flask installed. The non-server tests rely on this.
"""

import logging
import os

logger = logging.getLogger(__name__)

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
</style>
</head>
<body>
<table>
  <thead>
    <tr>
      <th>Company</th>
      <th>Title</th>
      <th>Resume</th>
      <th>Status</th>
      <th>JD</th>
      <th>Link</th>
      <th>Actions</th>
    </tr>
  </thead>
  <tbody>
  {% for row in rows %}
  <tr id="row-{{ row.id }}">
    <td class="company">{{ row.company }}</td>
    <td class="title">{{ row.title }}</td>
    <td>{{ row.resume_type or '' }}</td>
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
        <button class="btn-yes" onclick="decide('{{ row.id }}', {interested:'yes'})">&#10003; Yes</button>
        <button class="btn-no"  onclick="decide('{{ row.id }}', {interested:'no'})">&#10007; No</button>
        <button class="btn-applied" onclick="decide('{{ row.id }}', {applied:'true'})">Applied</button>
        <button class="btn-reset" onclick="doReset('{{ row.id }}')">&#8634; reset</button>
      </div>
    </td>
  </tr>
  {% else %}
  <tr><td colspan="7" style="text-align:center;padding:20px;color:#9ca3af;">No jobs to triage.</td></tr>
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

async function decide(jobId, payload) {
  payload.job_id = jobId;
  try {
    var resp = await fetch('/decide', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    if (resp.ok) {
      var data = await resp.json();
      setBadge(jobId, data.status);
    } else {
      var err = await resp.text();
      alert('Error ' + resp.status + ': ' + err);
    }
  } catch (e) {
    alert('Network error: ' + e);
  }
}

async function doReset(jobId) {
  try {
    var resp = await fetch('/reset', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({job_id: jobId})
    });
    if (resp.ok) {
      var data = await resp.json();
      setBadge(jobId, data.status);
    } else {
      var err = await resp.text();
      alert('Error ' + resp.status + ': ' + err);
    }
  } catch (e) {
    alert('Network error: ' + e);
  }
}
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(conn_factory):
    """Build and return the Flask app.

    Flask is imported here (lazily) so the module-level import of server.py
    does NOT require flask to be installed.
    """
    from flask import Flask, jsonify, render_template_string, request

    from .triage import apply_decision, fetch_triage_rows, reset_to_routed

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
        status_arg = request.args.get("status")
        resume_type_arg = request.args.get("resume_type") or None
        with conn_factory() as conn:
            if status_arg is not None:
                rows = fetch_triage_rows(conn, status=status_arg, resume_type=resume_type_arg)
            else:
                rows = fetch_triage_rows(
                    conn, statuses=("new", "routed"), resume_type=resume_type_arg
                )
        # Stringify id for template/JS (UUIDs need to be strings)
        for row in rows:
            row["id"] = str(row["id"])
        return render_template_string(INDEX_HTML, rows=rows)

    @app.post("/decide")
    def decide():
        if not request.is_json:
            return ("unsupported media type: expected application/json", 415)
        body = request.get_json()
        job_id = body.get("job_id")
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
        body = request.get_json()
        job_id = body.get("job_id")
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
