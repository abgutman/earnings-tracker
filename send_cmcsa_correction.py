#!/usr/bin/env python3
"""One-off: send corrected Comcast save-the-date email to all recipients.

Corrects the June 11 alert that captured call_date=2026-06-11 / 05:30 ET
(byline date) instead of the actual press release date: July 23, 2026 at 8:30 a.m. ET.

Usage: python3 send_cmcsa_correction.py
Requires GMAIL_USER and GMAIL_APP_PASSWORD in environment.
"""
import sys, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from email_utils import (
    send_email, subject_save_the_date, body_save_the_date,
    EMAIL_TO, GMAIL_USER, GMAIL_APP_PASSWORD, _html_email, SAVE_DATE_COLOR
)

TICKER = "CMCSA"
NAME = "Comcast"
CALL_DATE = "2026-07-23"
CALL_TIME = "08:30 ET"
SOURCE_URL = "https://finance.yahoo.com/markets/stocks/articles/comcast-host-second-quarter-2026-123000214.html"
SOURCE_TITLE = "Comcast to Host Second Quarter 2026 Earnings Conference Call"
PUBLISH_UNIX = 1781181000  # June 11, 2026 ~14:30 UTC

if not GMAIL_USER or not GMAIL_APP_PASSWORD:
    print("ERROR: GMAIL_USER and/or GMAIL_APP_PASSWORD not set in environment.")
    print("Export them and re-run:")
    print("  export GMAIL_USER=...")
    print("  export GMAIL_APP_PASSWORD=...")
    sys.exit(1)

# Build a custom subject that flags this as a correction
subject = f"✉️ CORRECTION: Save the date: {NAME}"

# Build a body that notes the correction
rows = [
    ("Earnings conference call", f"{CALL_DATE} at {CALL_TIME}"),
    ("Source", f"<em style='color:#495057;font-weight:400;font-size:13px;'>{SOURCE_TITLE}</em>"),
]
body = _html_email(
    header_bg=SAVE_DATE_COLOR,
    tag="✉️ Save the Date — CORRECTION",
    title="Corrected: Comcast Earnings Date",
    company=f"{NAME} ({TICKER})",
    blurb=(
        "<strong>This corrects the June 11 alert that carried the wrong date.</strong> "
        "The original alert listed June 11 — the press release's publish date — "
        "instead of the conference call date stated in the release. "
        "The correct date is below. We regret the error."
    ),
    rows=rows,
    cta_url=SOURCE_URL,
    cta_label="Read Press Release →",
    dashboard_url="https://abgutman.github.io/earnings-tracker/upcoming_earnings.html",
    dashboard_label="Earnings Dashboard",
    source_note="Source: Yahoo Finance / Business Wire",
)

print(f"Sending correction to: {EMAIL_TO}")
try:
    sent = send_email(subject, body)
    if sent:
        print(f"✉ Corrected alert sent successfully to {EMAIL_TO}")
    else:
        print("⚠ send_email returned False — check creds.")
        sys.exit(1)
except Exception as e:
    print(f"ERROR sending email: {e}")
    sys.exit(1)
