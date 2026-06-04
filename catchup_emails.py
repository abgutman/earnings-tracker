#!/usr/bin/env python3
"""One-shot: send catchup summary emails for last 24h of detected earnings events.

Usage:
  python3 catchup_emails.py           # dry run (prints what would be sent)
  python3 catchup_emails.py --live    # actually sends
"""
import json, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from email_utils import send_email, EMAIL_TO

HERE = Path(__file__).parent
ED = HERE / "earnings_data"

now = datetime.now(timezone.utc)
cutoff = now - timedelta(hours=24)
live = "--live" in sys.argv

# ── Collect EDGAR 8-K hits ────────────────────────────────────────────────────
cache = json.loads((ED / "cache.json").read_text())
edgar_hits = []
for ticker, info in cache.items():
    detected = info.get("last_8k_detected_at")
    if not detected:
        continue
    dt = datetime.fromisoformat(detected.replace("Z", "+00:00"))
    if dt >= cutoff:
        edgar_hits.append((ticker, info.get("name", ticker), info.get("last_8k_date", ""), info.get("last_8k_url", ""), dt))
edgar_hits.sort(key=lambda x: x[4], reverse=True)

# ── Collect save-the-date hits ────────────────────────────────────────────────
# Include everything in upcoming_dates.json with a future date — captures
# entries added before the captured_at field existed.
upcoming = json.loads((ED / "upcoming_dates.json").read_text())
today_iso = now.strftime("%Y-%m-%d")
std_hits = []
for ticker, entry in upcoming.items():
    if ticker.startswith("_") or not isinstance(entry, dict):
        continue
    primary = entry.get("release_date") or entry.get("call_date") or ""
    if primary < today_iso:
        continue
    std_hits.append((ticker, entry))
std_hits.sort(key=lambda x: x[1].get("release_date") or x[1].get("call_date") or "")

# ── Build + send EDGAR email ──────────────────────────────────────────────────
if edgar_hits:
    lines = [f"SEC earnings filings (8-K item 2.02) detected in the last 24 hours.\n"]
    for ticker, name, filed, url, dt in edgar_hits:
        lines.append(f"{name} ({ticker})")
        lines.append(f"  Filed:   {filed}")
        lines.append(f"  Filing:  {url}")
        lines.append("")
    lines.append("Dashboard: https://abgutman.github.io/av-tools/recent_earnings.html")
    subject = f"New earning reports: {len(edgar_hits)} filing{'s' if len(edgar_hits) > 1 else ''} in last 24h"
    body = "\n".join(lines)
    print(f"TO: {', '.join(EMAIL_TO)}")
    print(f"SUBJECT: {subject}")
    print(body)
    print("---")
    if live:
        ok = send_email(subject, body)
        print("✉ sent" if ok else "⚠ send failed (check credentials)")
    else:
        print("[dry run — pass --live to send]")
else:
    print("No new EDGAR 8-K filings in the last 24 hours.")

# ── Build + send save-the-date email ─────────────────────────────────────────
if std_hits:
    lines = [f"Save-the-date earnings announcements captured in the last 24 hours.\n"]
    for ticker, entry in std_hits:
        lines.append(f"{ticker}")
        if entry.get("release_date"):
            lines.append(f"  Release date: {entry['release_date']}")
        if entry.get("call_date"):
            lines.append(f"  Call date:    {entry['call_date']}")
        if entry.get("call_time"):
            lines.append(f"  Call time:    {entry['call_time']}")
        if entry.get("source_url"):
            lines.append(f"  Source:       {entry['source_url']}")
        lines.append("")
    lines.append("Dashboard: https://abgutman.github.io/av-tools/upcoming_earnings.html")
    subject = f"Save the date: {len(std_hits)} new earnings date{'s' if len(std_hits) > 1 else ''} in last 24h"
    body = "\n".join(lines)
    print(f"TO: {', '.join(EMAIL_TO)}")
    print(f"SUBJECT: {subject}")
    print(body)
    print("---")
    if live:
        ok = send_email(subject, body)
        print("✉ sent" if ok else "⚠ send failed (check credentials)")
    else:
        print("[dry run — pass --live to send]")
else:
    print("No new save-the-dates captured in the last 24 hours.")
