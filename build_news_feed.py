#!/usr/bin/env python3
"""Render news_feed.html from news_feed.json.

Social-media-style scrollable feed of Yahoo headlines for tracked Philadelphia
companies, newest first."""
import json, html
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).parent
ED = HERE / "earnings_data"
FEED_FILE = ED / "news_feed.json"
OUT = HERE / "news_feed.html"

from auth_gate import inject_auth

feed = json.loads(FEED_FILE.read_text()) if FEED_FILE.exists() else {"items": [], "generated_at": None}
items = feed.get("items", [])

ET = timezone(timedelta(hours=-4))
now = datetime.now(timezone.utc)

def esc(s): return html.escape(str(s) if s is not None else "")

def fmt_ago(unix):
    if not unix: return ""
    age = now - datetime.fromtimestamp(unix, tz=timezone.utc)
    secs = int(age.total_seconds())
    if secs < 60: return "just now"
    if secs < 3600: return f"{secs // 60}m"
    if secs < 24*3600: return f"{secs // 3600}h"
    return f"{secs // 86400}d"

def fmt_full_ts(unix):
    if not unix: return ""
    dt = datetime.fromtimestamp(unix, tz=timezone.utc).astimezone(ET)
    return dt.strftime("%a %b %-d, %-I:%M %p ET")

def render_card(item):
    tickers = item.get("tickers", [])
    ticker_html = "".join(f'<span class="chip">{esc(t)}</span>' for t in tickers[:6])
    if len(tickers) > 6:
        ticker_html += f'<span class="chip more">+{len(tickers)-6}</span>'
    company = ""
    if item.get("company"):
        company = f'<span class="company">{esc(item["company"])[:60]}</span>'
    ago = fmt_ago(item.get("published_unix",0))
    full_ts = fmt_full_ts(item.get("published_unix",0))
    publisher = esc(item.get("publisher",""))
    title = esc(item.get("title",""))
    link = esc(item.get("link",""))
    return f"""
    <article class="post">
      <div class="post-header">
        <div class="chips">{ticker_html}</div>
        <span class="ts" title="{full_ts}">{ago}</span>
      </div>
      <h3 class="headline"><a href="{link}" target="_blank" rel="noopener">{title}</a></h3>
      <div class="post-footer">
        <span class="publisher">{publisher}</span>
        {company}
      </div>
    </article>"""

posts_html = "".join(render_card(it) for it in items)
gen = feed.get("generated_at", "")
n_companies = len(set(t for it in items for t in it.get("tickers",[])))

html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Philly Business News Feed — Av's Tools</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Helvetica, Arial, sans-serif; margin: 0; background: #ecf0f3; color: #1a1a2e; }}
.feed-shell {{ max-width: 720px; margin: 24px auto; padding: 0 20px 60px; }}

.feed-header {{ background: white; padding: 18px 22px; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); margin-bottom: 16px; }}
.feed-header h1 {{ font-size: 22px; margin: 0 0 4px; }}
.feed-header .sub {{ font-size: 13px; color: #6c757d; margin: 0; }}
.feed-header .counts {{ display: flex; gap: 16px; margin-top: 12px; font-size: 12.5px; }}
.feed-header .counts span {{ color: #495057; }}
.feed-header .counts b {{ color: #1a1a2e; font-size: 14px; }}

.post {{ background: white; padding: 14px 18px; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); margin-bottom: 10px; transition: box-shadow 0.15s; }}
.post:hover {{ box-shadow: 0 2px 10px rgba(0,0,0,0.10); }}

.post-header {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 8px; }}
.chips {{ display: flex; gap: 5px; flex-wrap: wrap; }}
.chip {{ background: #eef0f3; color: #2c5282; font-family: ui-monospace, monospace; font-size: 11.5px; font-weight: 700; padding: 2px 8px; border-radius: 12px; }}
.chip.more {{ background: transparent; color: #6c757d; font-weight: 500; }}
.ts {{ color: #6c757d; font-size: 12px; white-space: nowrap; font-variant-numeric: tabular-nums; }}

.headline {{ font-size: 16px; line-height: 1.4; margin: 0 0 10px; font-weight: 600; }}
.headline a {{ color: #1a1a2e; text-decoration: none; }}
.headline a:hover {{ color: #2c5282; }}
.headline a:visited {{ color: #6c757d; }}

.post-footer {{ display: flex; align-items: center; gap: 10px; font-size: 12px; color: #6c757d; }}
.publisher {{ font-weight: 500; color: #495057; }}
.publisher::before {{ content: "via "; color: #adb5bd; font-weight: 400; }}
.company {{ font-style: italic; }}
.company::before {{ content: "·"; margin-right: 8px; color: #adb5bd; }}

.empty {{ background: white; padding: 40px; border-radius: 12px; text-align: center; color: #6c757d; font-style: italic; }}

@media (max-width: 600px) {{
  .feed-shell {{ padding: 0 12px 60px; }}
  .feed-header, .post {{ border-radius: 10px; }}
  .headline {{ font-size: 15px; }}
}}
</style>
</head>
<body>
<div class="feed-shell">
  <div class="feed-header">
    <h1>Philly Business News</h1>
    <p class="sub">Yahoo Finance headlines for the 100+ public companies HQ'd in the 8-county Philadelphia region. Last 48 hours, newest first.</p>
    <div class="counts">
      <span><b>{len(items)}</b> headlines</span>
      <span><b>{n_companies}</b> companies in news</span>
      <span>Refreshed hourly · last update {esc(gen[11:16] if gen else 'unknown')}</span>
    </div>
  </div>

  {posts_html if items else '<div class="empty">No news in the last 48 hours.</div>'}
</div>
</body>
</html>"""

OUT.write_text(inject_auth(html_doc))
print(f"Wrote {OUT} ({len(items)} items)")
