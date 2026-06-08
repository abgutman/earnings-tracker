#!/usr/bin/env python3
"""Shared email sender for earnings alert scripts.

TEMPLATES section: edit subject/body functions below to change email text.
"""
import os, smtplib, ssl
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

EMAIL_TO = ["agutman@inquirer.com", "EPalan@inquirer.com", "eravitch@inquirer.com"]
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

ET = timezone(timedelta(hours=-4))  # EDT (UTC-4)

def _fmt_et(val, fallback="unknown"):
    """Format an ISO string or unix timestamp to a human-readable ET time."""
    if not val:
        return fallback
    try:
        if isinstance(val, (int, float)):
            dt = datetime.fromtimestamp(val, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        return dt.astimezone(ET).strftime("%b %-d, %Y at %-I:%M %p ET")
    except Exception:
        return str(val)

def _html_email(header_bg, tag, title, company, blurb, rows, cta_url, cta_label, dashboard_url, source_note, dashboard_label="Earnings Dashboard", secondary_links=None):
    """Render a styled HTML email. rows = list of (label, value) tuples."""
    rows_html = "\n".join(
        f'      <tr>'
        f'<td style="padding:10px 0;color:#6c757d;font-size:14px;width:185px;border-top:1px solid #f0f0f0;vertical-align:top;">{lbl}</td>'
        f'<td style="padding:10px 0;color:#1a1a2e;font-size:14px;font-weight:500;border-top:1px solid #f0f0f0;vertical-align:top;">{val}</td>'
        f'</tr>'
        for lbl, val in rows
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:24px 16px;background:#eef0f3;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Helvetica,Arial,sans-serif;">
<div style="max-width:580px;margin:0 auto;">

  <div style="background:{header_bg};padding:28px 32px;border-radius:10px 10px 0 0;">
    <p style="margin:0 0 8px;color:rgba(255,255,255,0.6);font-size:11px;text-transform:uppercase;letter-spacing:1.5px;">{tag}</p>
    <h1 style="margin:0 0 8px;color:white;font-size:24px;font-weight:700;line-height:1.2;">{title}</h1>
    <p style="margin:0;color:rgba(255,255,255,0.9);font-size:17px;font-weight:600;">{company}</p>
  </div>

  <div style="background:white;padding:28px 32px;">
    <p style="margin:0 0 24px;color:#495057;font-size:13.5px;line-height:1.7;padding:14px 18px;background:#f8f9fa;border-left:4px solid {header_bg};border-radius:0 6px 6px 0;">{blurb}</p>
    <table style="width:100%;border-collapse:collapse;">
{rows_html}
    </table>
    <div style="margin:28px 0 22px;">
      <a href="{cta_url}" style="display:inline-block;background:{header_bg};color:white;padding:13px 26px;border-radius:7px;text-decoration:none;font-weight:700;font-size:14px;">{cta_label}</a>
    </div>
    <p style="margin:0 0 10px;font-size:13px;color:#868e96;">Also on the <a href="{dashboard_url}" style="color:{header_bg};font-weight:500;text-decoration:none;">{dashboard_label} ↗</a></p>
{"".join(f'    <p style="margin:0 0 10px;font-size:13px;color:#868e96;"><a href="{url}" style="color:{header_bg};font-weight:500;text-decoration:none;">{label} ↗</a></p>' for label, url in (secondary_links or []))}
    <p style="margin:0;font-size:13px;color:#868e96;">If you have any questions, comments, or concerns, reach out to Av.</p>
  </div>

  <div style="background:#f8f9fa;padding:16px 32px;border-top:1px solid #e9ecef;border-radius:0 0 10px 10px;">
    <p style="margin:0;font-size:12px;color:#adb5bd;line-height:1.6;">
      Av&#8217;s Tools &middot; Newsroom monitor &middot; Built with <a href="https://claude.ai" style="color:#adb5bd;">Claude</a> (Anthropic AI)<br>
      {source_note}
    </p>
  </div>

</div>
</body>
</html>"""


# ── TEMPLATES — edit subject/body functions below ────────────────────────────

EARNINGS_COLOR    = "#1a1a2e"   # dark navy — matches the dashboard
SAVE_DATE_COLOR   = "#1a5c3a"   # dark green — calendar / upcoming feel
BANKRUPTCY_COLOR  = "#8e1600"   # dark red/maroon

def subject_new_report(name, ticker):
    return f"\U0001f4c8 New earning report: {name}"   # 📈

def subject_save_the_date(name, ticker):
    return f"✉️ Save the date: {name}"      # ✉️

def body_new_report_edgar(name, ticker, filing_date, url, accepted_at=None, detected_at=None):
    rows = []
    if accepted_at:
        rows.append(("Submitted to SEC", _fmt_et(accepted_at)))
    if detected_at:
        rows.append(("Detected by monitor", _fmt_et(detected_at)))
    rows.append(("Filed with SEC", filing_date))
    return _html_email(
        header_bg=EARNINGS_COLOR,
        tag="\U0001f4c8 Earnings Alert",
        title="New Earning Report",
        company=f"{name} ({ticker})",
        blurb=(
            "This alert was generated automatically by <strong>Claude (Anthropic AI)</strong>, "
            "which monitors SEC EDGAR for 8-K filings from public companies headquartered in the "
            "Philadelphia region at Av's request. An <strong>8-K item&nbsp;2.02</strong> is the formal SEC submission "
            "of a quarterly earnings press release &mdash; typically filed within minutes of the wire release. "
            "<strong>Note:</strong> This is the SEC notification that a press release was issued, not the full earnings report. "
            "The press release is linked at the bottom of this filing (under Item&nbsp;9.01). "
            "The formal quarterly report (10-Q) will appear on the dashboard when available &mdash; typically within a few hours to one business day."
        ),
        rows=rows,
        cta_url=url,
        cta_label="View SEC Filing →",
        dashboard_url="https://abgutman.github.io/av-tools/recent_earnings.html",
        source_note="Source: SEC EDGAR &mdash; <a href='https://data.sec.gov' style='color:#adb5bd;'>data.sec.gov</a>",
    )

def body_new_report_wire(name, ticker, published_unix, publisher, headline, url):
    rows = [
        ("Publicly available",  _fmt_et(published_unix)),
        ("Publisher",           publisher),
        ("Headline",            f"<em style='color:#495057;font-weight:400;'>{headline[:180]}</em>"),
    ]
    return _html_email(
        header_bg=EARNINGS_COLOR,
        tag="\U0001f4c8 Earnings Alert",
        title="New Earning Report",
        company=f"{name} ({ticker})",
        blurb=(
            "This alert was generated automatically by <strong>Claude (Anthropic AI)</strong>, "
            "monitoring Yahoo Finance for wire press releases from Philadelphia-region public companies "
            "at Av's request. This item matched our wire-publisher and earnings-keyword filters &mdash; it may be the "
            "actual results release, a save-the-date announcement, or a related filing. "
            "<strong>Read the headline to determine which.</strong>"
        ),
        rows=rows,
        cta_url=url,
        cta_label="Read Article →",
        dashboard_url="https://abgutman.github.io/av-tools/recent_earnings.html",
        source_note="Source: Yahoo Finance / wire services (Business Wire, GlobeNewswire, PR Newswire)",
    )

def body_save_the_date(name, ticker, release_date, call_date, call_time, source_url, headline, published_unix=None):
    rows = []
    if published_unix:
        rows.append(("Article published", _fmt_et(published_unix)))
    if release_date:
        rows.append(("Earnings release", release_date))
    if call_date:
        val = f"{call_date} at {call_time}" if call_time else call_date
        rows.append(("Conference call", val))
    rows.append(("Source", f"<em style='color:#495057;font-weight:400;font-size:13px;'>{headline[:180]}</em>"))
    return _html_email(
        header_bg=SAVE_DATE_COLOR,
        tag="✉️ Save the Date",
        title="Earnings Date Announced",
        company=f"{name} ({ticker})",
        blurb=(
            "This alert was generated automatically by <strong>Claude (Anthropic AI)</strong>, "
            "which scans wire press releases from Philadelphia-region public companies for earnings "
            "date announcements at Av's request. The dates below were extracted from a press release "
            "published on the wire services."
        ),
        rows=rows,
        cta_url=source_url,
        cta_label="Read Press Release →",
        dashboard_url="https://abgutman.github.io/av-tools/upcoming_earnings.html",
        source_note="Source: Yahoo Finance / wire services (Business Wire, GlobeNewswire, PR Newswire)",
    )


def subject_bankruptcy_alert(debtor_name):
    return f"\U0001f4a5 New Chapter 11: {debtor_name}"   # 💥

def body_bankruptcy_alert(debtor_name, court, date_filed, debtor_zip, region,
                          courtlistener_url, pacer_url=None, docket_id=None):
    rows = [
        ("Date filed",  date_filed),
        ("Court",       court),
        ("Debtor zip",  f"{debtor_zip} ({region})"),
    ]
    detail_url = f"https://abgutman.github.io/av-tools/bankruptcy_cases/{docket_id}.html" if docket_id else None
    cta_url = detail_url or courtlistener_url or "#"
    secondary = []
    if courtlistener_url:
        secondary.append(("View on CourtListener", courtlistener_url))
    return _html_email(
        header_bg=BANKRUPTCY_COLOR,
        tag="\U0001f4a5 Bankruptcy Alert",
        title="New Chapter 11 Filing",
        company=debtor_name,
        blurb=(
            "This alert was generated automatically by <strong>Claude (Anthropic AI)</strong>, "
            "which monitors federal bankruptcy courts nationwide for Chapter 11 filings from "
            "companies in the Philadelphia region at Av's request. The debtor's address in "
            "court records matched a zip code in the 8-county Philadelphia region."
        ),
        rows=rows,
        cta_url=cta_url,
        cta_label="View case details &rarr;",
        dashboard_url="https://abgutman.github.io/av-tools/bankruptcy_dashboard.html",
        dashboard_label="Bankruptcy Dashboard",
        source_note="Source: CourtListener / RECAP &mdash; <a href='https://www.courtlistener.com' style='color:#adb5bd;'>courtlistener.com</a>",
        secondary_links=secondary,
    )


# ── Sender ────────────────────────────────────────────────────────────────────

def send_email(subject, body, log_fn=None, to=None):
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        if log_fn:
            log_fn(f"⚠ No Gmail creds; would have sent: {subject}")
        return False
    recipients = to if to is not None else EMAIL_TO
    if isinstance(recipients, str):
        recipients = [recipients]
    msg = MIMEMultipart("alternative")
    msg["From"] = GMAIL_USER
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "html"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.sendmail(GMAIL_USER, recipients, msg.as_string())
    return True
