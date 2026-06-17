"""Nightly digest — summarize what's new and relevant, print it, and optionally email it.

Read-only over the jobs table. Designed to run from CI (a daily GitHub Actions workflow) so the
operator gets a once-a-day email; also runnable locally (`python -m jobmaxxing.report`).
"""

import argparse
import html
import logging
import os
import smtplib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import psycopg
from psycopg.rows import dict_row

from .config import load_settings
from .normalize import in_window_term_labels, off_window_sql

logger = logging.getLogger(__name__)

# How far back "new" reaches, and how many roles to list in the email body.
NEW_WINDOW_HOURS = 24
NEW_ROWS_CAP = 25


@dataclass
class Digest:
    now: datetime
    window: list[str]            # in-window labels, e.g. ["Fall 2026", ...]
    new_count: int               # routed + undecided, first-seen in last 24h, in-window
    new_rows: list[dict]         # capped at NEW_ROWS_CAP
    by_type: dict[str, int]      # new rows grouped by resume_type
    total_undecided: int         # all routed + undecided in-window (the backlog)
    queue_size: int              # nightly_queue: relevant roles still missing a JD


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_digest(conn: psycopg.Connection, now: datetime) -> Digest:
    """Compute the daily digest as of ``now`` (read-only)."""
    labels = sorted(in_window_term_labels(now.date()))
    # "visible" = routed, and NOT an off-window github row (same predicate triage demotes on, so the
    # digest and the UI agree on what's relevant). Off-window/legacy rows are excluded entirely here.
    visible = f"resume_type is not null and not ({off_window_sql(labels)})"
    undecided = f"{visible} and status in ('new', 'routed')"
    since = now - timedelta(hours=NEW_WINDOW_HOURS)

    with conn.cursor(row_factory=dict_row) as cur:
        new_rows = cur.execute(
            f"select company, title, resume_type, term, url, route_confidence, posted_at "
            f"from jobs where {undecided} and first_seen_at >= %s "
            f"order by route_confidence desc nulls last, posted_at desc nulls last limit %s",
            (since, NEW_ROWS_CAP),
        ).fetchall()
        new_count = cur.execute(
            f"select count(*) as n from jobs where {undecided} and first_seen_at >= %s", (since,)
        ).fetchone()["n"]
        total_undecided = cur.execute(
            f"select count(*) as n from jobs where {undecided}"
        ).fetchone()["n"]
        by_type_rows = cur.execute(
            f"select resume_type, count(*) as n from jobs where {undecided} and first_seen_at >= %s "
            f"group by resume_type order by n desc, resume_type", (since,)
        ).fetchall()
        queue_size = cur.execute("select count(*) as n from nightly_queue").fetchone()["n"]

    new = [{**r, "term": list(r["term"]) if r["term"] else []} for r in new_rows]
    by_type = {r["resume_type"]: r["n"] for r in by_type_rows}
    return Digest(now=now, window=labels, new_count=new_count, new_rows=new,
                  by_type=by_type, total_undecided=total_undecided, queue_size=queue_size)


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def subject(d: Digest) -> str:
    return f"jobmaxxing daily — {d.new_count} new in-window roles"


def _conf(value) -> str:
    return f"{value:.2f}" if value is not None else "—"


def render_text(d: Digest) -> str:
    lines = [
        f"jobmaxxing daily — {d.new_count} new in-window role(s) in the last "
        f"{NEW_WINDOW_HOURS}h",
        f"window: {', '.join(d.window)}",
        "",
    ]
    if d.by_type:
        lines.append("new by type: " + ", ".join(f"{k}: {v}" for k, v in d.by_type.items()))
    lines.append(f"undecided in-window backlog: {d.total_undecided}")
    lines.append(f"manual-capture queue (relevant, JD-less): {d.queue_size}")
    lines.append("")
    if d.new_rows:
        lines.append(f"new roles (top {len(d.new_rows)} of {d.new_count}):")
        for r in d.new_rows:
            term = ", ".join(r["term"]) if r["term"] else "—"
            lines.append(
                f"  • {r['company']} — {r['title']}  "
                f"[{r['resume_type']} · {term} · conf {_conf(r['route_confidence'])}]"
            )
            lines.append(f"    {r['url']}")
    else:
        lines.append("no new in-window roles in the last 24h.")
    return "\n".join(lines)


def render_html(d: Digest) -> str:
    def esc(value) -> str:
        return html.escape(str(value if value is not None else ""))

    rows = ""
    for r in d.new_rows:
        term = ", ".join(r["term"]) if r["term"] else "—"
        rows += (
            "<tr>"
            f"<td>{esc(r['company'])}</td>"
            f"<td><a href=\"{esc(r['url'])}\">{esc(r['title'])}</a></td>"
            f"<td>{esc(r['resume_type'])}</td>"
            f"<td>{esc(term)}</td>"
            f"<td style=\"text-align:right\">{esc(_conf(r['route_confidence']))}</td>"
            "</tr>"
        )
    by_type = ", ".join(f"{esc(k)}: {v}" for k, v in d.by_type.items()) or "—"
    table = (
        "<table border=\"1\" cellpadding=\"6\" cellspacing=\"0\" "
        "style=\"border-collapse:collapse;font-family:system-ui,sans-serif;font-size:13px\">"
        "<thead><tr><th>Company</th><th>Title</th><th>Type</th><th>Term</th><th>Conf</th></tr>"
        f"</thead><tbody>{rows}</tbody></table>"
        if d.new_rows else "<p>No new in-window roles in the last 24h.</p>"
    )
    return (
        "<html><body style=\"font-family:system-ui,sans-serif\">"
        f"<h2>jobmaxxing daily — {d.new_count} new in-window roles</h2>"
        f"<p><b>window:</b> {esc(', '.join(d.window))}<br>"
        f"<b>new by type:</b> {by_type}<br>"
        f"<b>undecided in-window backlog:</b> {d.total_undecided}<br>"
        f"<b>manual-capture queue (relevant, JD-less):</b> {d.queue_size}</p>"
        f"{table}"
        "</body></html>"
    )


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

@dataclass
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    sender: str
    recipients: list[str]


_REQUIRED_SMTP_ENV = ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "REPORT_TO")


def load_smtp_config(env=None) -> SmtpConfig:
    """Build SmtpConfig from environment. Generic SMTP, so Gmail (smtp.gmail.com) and Outlook
    (smtp.office365.com) both work by config alone. REPORT_TO is a comma-separated recipient list."""
    env = os.environ if env is None else env
    missing = [k for k in _REQUIRED_SMTP_ENV if not env.get(k)]
    if missing:
        raise RuntimeError(f"missing required SMTP env vars: {', '.join(missing)}")
    user = env["SMTP_USER"]
    recipients = [a.strip() for a in env["REPORT_TO"].split(",") if a.strip()]
    if not recipients:
        raise RuntimeError("REPORT_TO contained no addresses")
    return SmtpConfig(
        host=env["SMTP_HOST"],
        port=int(env.get("SMTP_PORT") or 587),
        user=user,
        password=env["SMTP_PASS"],
        sender=env.get("SMTP_FROM") or user,
        recipients=recipients,
    )


def send_digest(digest: Digest, cfg: SmtpConfig, *, smtp_factory=smtplib.SMTP) -> None:
    """Send the digest as a multipart text+html email over STARTTLS. ``smtp_factory`` is injectable
    so tests can substitute a fake transport (no real network)."""
    msg = EmailMessage()
    msg["Subject"] = subject(digest)
    msg["From"] = cfg.sender
    msg["To"] = ", ".join(cfg.recipients)
    msg.set_content(render_text(digest))
    msg.add_alternative(render_html(digest), subtype="html")
    with smtp_factory(cfg.host, cfg.port) as smtp:
        smtp.starttls()
        smtp.login(cfg.user, cfg.password)
        smtp.send_message(msg)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="jobmaxxing.report")
    parser.add_argument("--email", action="store_true", help="send the digest by email")
    args = parser.parse_args(argv)

    settings = load_settings()
    now = datetime.now(timezone.utc)
    with psycopg.connect(settings.database_url) as conn:
        digest = build_digest(conn, now)

    print(render_text(digest))
    if args.email:
        cfg = load_smtp_config()
        send_digest(digest, cfg)
        logger.info("emailed digest to %s", ", ".join(cfg.recipients))


if __name__ == "__main__":
    main()
