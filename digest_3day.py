#!/usr/bin/env python3
"""3-day hits digest for the local-company news feed.

Runs every ~3 days (GitHub Actions). Emails ONLY Av, and ONLY when there's
something to say:
  - one or more tracked companies got a new item in the last 3 days (a "hit"), OR
  - the feed looks stale (nothing new anywhere in >14 days — a break signal).
No hits + healthy feed => no email (no empty digests).

The email also lists the companies we could NOT find a source for (the
watchlist), so they stay visible during this one-month trial.

--live actually sends; without it, logs what it would send.

Trial: stood up 2026-07-19, revisit ~2026-08-19.
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parents[2]))  # repo root fallback (dev tree)
from email_utils import send_email

HERE = Path(__file__).parent
ED = HERE / "earnings_data"
FEED_FILE = ED / "news_feed.json"
ROSTER_FILE = ED / "local_roster.json"

WINDOW_DAYS = 3
STALE_DAYS = 14
TO = "agutman@inquirer.com"


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", file=sys.stderr)


def _fmt(unix):
    if not unix:
        return ""
    return datetime.fromtimestamp(unix, tz=timezone.utc).astimezone(
        timezone.utc).strftime("%b %-d")


def build():
    feed = json.loads(FEED_FILE.read_text())
    roster = json.loads(ROSTER_FILE.read_text())
    items = feed.get("items", [])
    now = time.time()
    cutoff = now - WINDOW_DAYS * 86400

    # hits: items published within the window, grouped by company
    hits = {}
    for it in items:
        if it.get("published_unix", 0) >= cutoff:
            hits.setdefault(it["company"], []).append(it)
    for co in hits:
        hits[co].sort(key=lambda x: x.get("published_unix", 0), reverse=True)

    newest = max((it.get("published_unix", 0) for it in items), default=0)
    stale = newest and (now - newest) > STALE_DAYS * 86400

    # watchlist: roster companies we couldn't find a source for (active:false, no
    # sources) — excluding deliberate drops (e.g. Philadelphia Magazine, a media
    # outlet) which we DID find but chose not to track.
    watch = [e["name"] for e in roster.get("entities", [])
             if not e.get("active") and not e.get("sources")
             and not (e.get("note", "").startswith("EXCLUDED"))]

    return hits, stale, newest, watch, len(items)


def render(hits, stale, newest, watch, total):
    n_hit_items = sum(len(v) for v in hits.values())
    head_bg = "#8e1600" if stale else "#1a1a2e"
    banner = ("⚠ Feed may be stale — nothing new in the last "
              f"{STALE_DAYS}+ days. Check the workflow." if stale else
              f"{n_hit_items} new item(s) across {len(hits)} compan(y/ies) in the last {WINDOW_DAYS} days.")

    rows = ""
    for co in sorted(hits, key=lambda c: -max(i["published_unix"] for i in hits[c])):
        lis = "".join(
            f'<li style="margin:4px 0;"><a href="{it.get("link","")}" '
            f'style="color:#1a5276;text-decoration:none;">{it.get("title","")}</a> '
            f'<span style="color:#888;font-size:12px;">— {it.get("publisher","")}, {_fmt(it.get("published_unix"))}</span></li>'
            for it in hits[co])
        rows += (f'<div style="margin:14px 0;"><div style="font-weight:700;color:#0a1628;">{co}</div>'
                 f'<ul style="margin:6px 0 0;padding-left:20px;">{lis}</ul></div>')
    if not hits:
        rows = '<p style="color:#666;">No tracked company had a new item in the last 3 days.</p>'

    watch_html = ", ".join(watch)
    return f"""<html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;color:#1a1a1a;max-width:640px;">
<div style="background:{head_bg};color:#fff;padding:16px 20px;border-radius:8px 8px 0 0;">
  <div style="font-size:12px;letter-spacing:.5px;opacity:.8;">LOCAL COMPANY NEWS · 3-DAY DIGEST</div>
  <div style="font-size:16px;font-weight:700;margin-top:4px;">{banner}</div>
</div>
<div style="padding:18px 20px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;">
  {rows}
  <div style="margin-top:22px;padding-top:14px;border-top:1px solid #eee;font-size:12.5px;color:#555;">
    <b>Still no source found ({len(watch)})</b> — companies in the roster we couldn't wire to a feed:<br>
    <span style="color:#777;">{watch_html}</span>
  </div>
  <div style="margin-top:16px;font-size:11.5px;color:#999;">
    Feed: {total} items in the 30-day window · newest {_fmt(newest)} ·
    <a href="https://abgutman.github.io/earnings-tracker/news_feed.html" style="color:#1a5276;">open the feed</a><br>
    Press releases are companies' own announcements — verify against source material. One-month trial.
  </div>
</div></body></html>"""


def main():
    live = "--live" in sys.argv
    hits, stale, newest, watch, total = build()
    if not hits and not stale:
        log(f"No hits in {WINDOW_DAYS}d and feed healthy (newest {_fmt(newest)}) — no email.")
        return
    subject = ("⚠ Local news feed may be stale" if stale
               else f"Local news: {sum(len(v) for v in hits.values())} new item(s), "
                    f"{len(hits)} compan(y/ies) — 3-day digest")
    body = render(hits, stale, newest, watch, total)
    if live:
        ok = send_email(subject, body, log_fn=log, to=TO)
        log(f"sent={ok} to {TO}: {subject}")
    else:
        log(f"[dry-run] would send to {TO}: {subject}")
        log(f"  hits={ {k: len(v) for k, v in hits.items()} } stale={stale} watch={len(watch)}")


if __name__ == "__main__":
    main()
