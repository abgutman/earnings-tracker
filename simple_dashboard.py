#!/usr/bin/env python3
"""Two earnings pages, focused for newsroom scanning:

  recent_earnings.html   — each company's MOST RECENT 8-K item 2.02 filing
                           (last 90 days only). EDGAR source of truth — Yahoo
                           events excluded so save-the-dates can't sneak in.
  upcoming_earnings.html — confirmed save-the-dates only. Release info + call
                           info per row.

Past-due / dormant filers (no 8-K item 2.02 in 90+ days) are linked at the
bottom of each page in a small diagnostic note — kept around but not noise.
"""
import json, html
from pathlib import Path
from datetime import datetime, timezone, timedelta

ET = timezone(timedelta(hours=-4))  # EDT (UTC-4); shifts to UTC-5 in winter but close enough

HERE = Path(__file__).parent
ED = HERE / "earnings_data"
CACHE_FILE = ED / "cache.json"
UPCOMING_FILE = ED / "upcoming_dates.json"
COMPANIES_FILE = ED / "expanded_companies.json"

from auth_gate import inject_auth

cache = json.loads(CACHE_FILE.read_text())
upcoming_raw = json.loads(UPCOMING_FILE.read_text()) if UPCOMING_FILE.exists() else {}
companies = json.loads(COMPANIES_FILE.read_text())

company_by_ticker = {}
for c in companies:
    for t in (c.get("tickers") or [c.get("ticker_hint","")]):
        if t: company_by_ticker[t] = c

today = datetime.now(timezone.utc)
today_date = today.date()

def esc(s): return html.escape(str(s) if s is not None else "")

def fmt_date(iso):
    if not iso: return "—"
    try:
        if "T" in iso:
            d = datetime.fromisoformat(iso.replace("Z","+00:00"))
        else:
            d = datetime.strptime(iso, "%Y-%m-%d")
        return d.strftime("%b %-d, %Y")
    except: return iso[:10]

def yahoo_search_url(ticker):
    return f"https://finance.yahoo.com/quote/{ticker}/news"

def edgar_filings_url(cik):
    if not cik: return None
    return f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={int(cik):010d}&type=10-Q,10-K,8-K&dateb=&owner=include&count=20"

# =================== RECENT PAGE — last 90 days of EDGAR 8-K item 2.02 ===================

def build_recent_rows():
    """For each company in cache, take their last_8k_date (from EDGAR submissions),
    and include them if within the last 90 days. EDGAR-only — never Yahoo."""
    cutoff = (today_date - timedelta(days=90)).isoformat()
    twenty_four_hours_ago = today - timedelta(hours=24)
    out = []
    for tk, v in cache.items():
        last_8k = v.get("last_8k_date","")
        if not last_8k or last_8k < cutoff: continue
        try:
            d = datetime.strptime(last_8k, "%Y-%m-%d").date()
        except: continue
        days = (today_date - d).days
        # NEW detection: was this filing detected by the poller in the last 24h?
        is_new = False
        detected_at = v.get("last_8k_detected_at","")
        if detected_at:
            try:
                det = datetime.fromisoformat(detected_at.replace("Z","+00:00"))
                is_new = det >= twenty_four_hours_ago
            except: pass
        out.append({
            "ticker": tk,
            "name": v.get("name",""),
            "cik": v.get("cik"),
            "filing_date": last_8k,
            "days": days,
            "edgar_url": v.get("last_8k_url",""),
            "is_new": is_new,
            "detected_at": detected_at,
        })
    # Sort: NEW rows first, then by filing date desc
    out.sort(key=lambda x: (not x["is_new"], -datetime.strptime(x["filing_date"], "%Y-%m-%d").toordinal()))
    return out

def render_recent_row(r):
    edgar_link = ""
    if r.get("edgar_url"):
        edgar_link = f'<a href="{esc(r["edgar_url"])}" target="_blank" rel="noopener">View 8-K filing</a>'
    elif r.get("cik"):
        edgar_link = f'<a href="{esc(edgar_filings_url(r["cik"]))}" target="_blank" rel="noopener">View 8-K filing</a>'
    yahoo_link = f'<a href="{esc(yahoo_search_url(r["ticker"]))}" target="_blank" rel="noopener">Yahoo News</a>'
    new_badge = '<span class="badge-new">NEW</span> ' if r.get("is_new") else ""
    row_class = "row-new" if r.get("is_new") else ""
    return f"""
    <tr class="{row_class}">
      <td class="date">{new_badge}<b>{fmt_date(r["filing_date"])}</b><br><span class="dim">{r["days"]}d ago</span></td>
      <td class="tk">{esc(r["ticker"])}</td>
      <td class="nm">{esc(r["name"])}</td>
      <td class="links">{edgar_link} · {yahoo_link}</td>
    </tr>"""

def build_recent_page():
    rows = build_recent_rows()
    new_count = sum(1 for r in rows if r.get("is_new"))
    body_rows = "".join(render_recent_row(r) for r in rows)
    table_html = f"<table><thead><tr><th>Filed</th><th>Ticker</th><th>Company</th><th>Links</th></tr></thead><tbody>{body_rows}</tbody></table>"
    new_stat = f'<div class="stat new-stat"><b>{new_count}</b> new in last 24h</div>' if new_count else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Recent earnings — Av's Tools</title>{COMMON_STYLES}</head>
<body><div class="container">
  <h1>Recent earnings reports</h1>
  <p class="meta">Each company's most recent SEC 8-K item 2.02 filing (the actual quarterly earnings release), if filed in the last 90 days. EDGAR source of truth. Newest first. Updated {datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")}.</p>
  {render_tabs(active="recent")}
  <div class="stats">
    <div class="stat"><b>{len(rows)}</b> companies filed in last 90 days</div>
    {new_stat}
  </div>
  {table_html if rows else '<p class="empty">No 8-K item 2.02 filings in the last 90 days.</p>'}
  {build_dormant_note()}
</div></body></html>"""

# =================== UPCOMING PAGE — confirmed dates only ===================

def build_upcoming_rows():
    out = []
    for tk, entry in upcoming_raw.items():
        if tk.startswith("_"): continue
        if not isinstance(entry, dict): continue
        release = entry.get("release_date")
        call_d = entry.get("call_date")
        if not release and not call_d: continue
        # Skip past dates
        primary = release or call_d
        if primary < today_date.isoformat(): continue
        comp = company_by_ticker.get(tk, {})
        cache_entry = cache.get(tk, {})
        out.append({
            "ticker": tk,
            "name": cache_entry.get("name", comp.get("name","")) or entry.get("name",""),
            "cik": cache_entry.get("cik", comp.get("cik")),
            "release_date": release,
            "release_time": entry.get("release_time"),
            "call_date": call_d,
            "call_time": entry.get("call_time"),
            "source_url": entry.get("source_url",""),
        })
    out.sort(key=lambda r: r["release_date"] or r["call_date"] or "9999")
    return out

def render_upcoming_row(r):
    primary = r.get("release_date") or r.get("call_date")
    try:
        d = datetime.strptime(primary, "%Y-%m-%d").date()
        days_until = (d - today_date).days
    except: days_until = None

    if days_until is None:
        when_label = "—"
    elif days_until < 0:
        when_label = f'<span class="overdue">{abs(days_until)}d ago</span>'
    elif days_until == 0:
        when_label = '<span class="today">TODAY</span>'
    elif days_until <= 7:
        when_label = f'<span class="soon">in {days_until}d</span>'
    else:
        when_label = f'in {days_until}d'

    # Release info column: date + time
    rel = ""
    if r.get("release_date"):
        rel_t = f' · {esc(r["release_time"])}' if r.get("release_time") else ""
        rel = f'Release: {fmt_date(r["release_date"])}{rel_t}'
    # Call info
    call = ""
    if r.get("call_date"):
        call_t = f' · {esc(r["call_time"])}' if r.get("call_time") else ""
        call = f'Call: {fmt_date(r["call_date"])}{call_t}'
    release_info = (rel + (("<br>" + call) if call else "")) if (rel or call) else "—"

    src_link = ""
    if r.get("source_url"):
        src_link = f'<a href="{esc(r["source_url"])}" target="_blank" rel="noopener">Source</a> · '
    edgar_link = ""
    if r.get("cik"):
        edgar_link = f'<a href="{esc(edgar_filings_url(r["cik"]))}" target="_blank" rel="noopener">EDGAR</a>'

    return f"""
    <tr>
      <td class="date"><b>{fmt_date(primary)}</b></td>
      <td class="when">{when_label}</td>
      <td class="tk">{esc(r["ticker"])}</td>
      <td class="nm">{esc(r["name"])}</td>
      <td class="rel-info">{release_info}</td>
      <td class="links">{src_link}{edgar_link}</td>
    </tr>"""

def build_upcoming_page():
    rows = build_upcoming_rows()
    body_rows = "".join(render_upcoming_row(r) for r in rows)
    table_html = f"<table><thead><tr><th>Date</th><th>When</th><th>Ticker</th><th>Company</th><th>Release information</th><th>Links</th></tr></thead><tbody>{body_rows}</tbody></table>"
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Upcoming earnings — Av's Tools</title>{COMMON_STYLES}</head>
<body><div class="container">
  <h1>Upcoming earnings reports</h1>
  <p class="meta">Confirmed earnings releases and conference calls, sourced from save-the-date press releases and manual entries. Soonest first. Updated {datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")}.</p>
  {render_tabs(active="upcoming")}
  <div class="stats"><div class="stat"><b>{len(rows)}</b> confirmed upcoming events</div></div>
  {table_html if rows else '<p class="empty">No confirmed upcoming earnings dates. Add entries to earnings_data/upcoming_dates.json as you learn them.</p>'}
  {build_dormant_note()}
</div></body></html>"""

# =================== Dormant filers note ===================

def build_dormant_note():
    """Companies whose last 8-K item 2.02 is older than 90 days. Listed compactly
    as a footnote so reporters know they're in our database but aren't recently active."""
    dormant = []
    cutoff = (today_date - timedelta(days=90)).isoformat()
    for tk, v in cache.items():
        last_8k = v.get("last_8k_date","")
        if not last_8k: continue
        if last_8k >= cutoff: continue
        try:
            d = datetime.strptime(last_8k, "%Y-%m-%d").date()
            days = (today_date - d).days
        except: continue
        dormant.append({"ticker": tk, "name": v.get("name",""), "days": days, "last": last_8k})
    dormant.sort(key=lambda x: -x["days"])
    if not dormant: return ""
    items = []
    for d in dormant[:60]:  # cap the display
        items.append(f'<span class="dormant-item"><b>{esc(d["ticker"])}</b> {esc(d["name"])[:32]} <span class="dim">({d["days"]}d)</span></span>')
    extra = f" + {len(dormant)-60} more" if len(dormant) > 60 else ""
    return f"""
    <details class="dormant">
      <summary>Filers with no 8-K item 2.02 in 90+ days ({len(dormant)})</summary>
      <p class="dim" style="margin: 6px 0 10px; font-size: 12px;">These are companies in our list whose last SEC quarterly-earnings filing is over 90 days old — may be inactive, delinquent, or off the typical cadence (e.g., 20-F filers, micro-caps). Listed compactly so we don't lose track of them but they don't clutter the upcoming view.</p>
      <div class="dormant-grid">{"".join(items)}</div>
      <p class="dim" style="font-size: 11px; margin-top: 8px;">{extra}</p>
    </details>"""

# =================== Shared chrome ===================

def render_tabs(active):
    return f"""
  <div class="tabs">
    <a href="recent_earnings.html"{' class="active"' if active=='recent' else ''}>Recent earnings</a>
    <a href="upcoming_earnings.html"{' class="active"' if active=='upcoming' else ''}>Upcoming earnings</a>
    <a href="earnings_notes.html"{' class="active"' if active=='notes' else ''}>Notes</a>
  </div>"""

def build_notes_page():
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Notes — Earnings Reports — Av's Tools</title>{COMMON_STYLES}
<style>
.notes-content {{ background: white; padding: 26px 32px; border-radius: 6px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); line-height: 1.65; max-width: 900px; }}
.notes-content h2 {{ margin: 22px 0 8px; font-size: 17px; padding-bottom: 6px; border-bottom: 2px solid #ececef; color: #1a1a2e; }}
.notes-content h2:first-of-type {{ margin-top: 0; }}
.notes-content h3 {{ margin: 18px 0 6px; font-size: 13.5px; color: #495057; text-transform: uppercase; letter-spacing: 0.4px; }}
.notes-content p, .notes-content li {{ font-size: 14px; color: #2d3436; }}
.notes-content ul {{ margin: 4px 0 10px; padding-left: 22px; }}
.notes-content li {{ margin: 3px 0; }}
.notes-content code {{ background: #f1f3f5; padding: 1px 6px; border-radius: 3px; font-size: 12.5px; color: #d63031; }}
.notes-content strong {{ color: #1a1a2e; }}
.notes-content table.schedule {{ width: 100%; margin: 8px 0 12px; font-size: 13px; }}
.notes-content table.schedule th {{ background: #f1f3f5; color: #1a1a2e; text-transform: none; letter-spacing: normal; font-size: 12.5px; padding: 6px 10px; }}
.notes-content table.schedule td {{ font-size: 13px; padding: 6px 10px; }}
.callout {{ background: #fff9e3; padding: 10px 14px; border-left: 3px solid #ffd966; border-radius: 0 4px 4px 0; margin: 8px 0; font-size: 13px; }}
.callout b {{ color: #b08200; }}
</style></head>
<body><div class="container">
  <h1>About these pages</h1>
  <p class="meta">How the Recent and Upcoming earnings pages are populated, and when they update.</p>
  {render_tabs(active="notes")}

  <div class="notes-content">

    <h2>Recent earnings reports</h2>

    <h3>What it shows</h3>
    <p>Each Philadelphia-area public company's most recent <strong>SEC 8-K item 2.02</strong> filing — the formal SEC filing that contains the actual quarterly earnings release. Only filings from the last 90 days are shown.</p>

    <h3>Where the data comes from</h3>
    <p><strong>SEC EDGAR</strong> (the canonical record). We fetch each company's filing history directly from <code>data.sec.gov/submissions/CIK*.json</code>, identify the most recent 8-K with item 2.02 listed, and show it.</p>

    <h3>How often it updates</h3>
    <p>15 polling runs every weekday, clustered around when companies typically release earnings:</p>
    <table class="schedule">
      <thead><tr><th>Window</th><th>Frequency</th><th>Why</th></tr></thead>
      <tbody>
        <tr><td>7:30 AM &ndash; 9:30 AM ET</td><td>Every 15 min (9 polls)</td><td>Pre-market: most large filers drop earnings here</td></tr>
        <tr><td>Noon ET &amp; 2 PM ET</td><td>Sporadic (2 polls)</td><td>Catches mid-day filings (mostly REITs and utilities)</td></tr>
        <tr><td>4:00 PM &ndash; 5:00 PM ET</td><td>Every 15 min (5 polls)</td><td>Post-market: the other major release window</td></tr>
      </tbody>
    </table>

    <div class="callout"><b>NEW badge:</b> When a polling run detects an 8-K item 2.02 we hadn't seen before, that row gets a red <strong>NEW</strong> badge and yellow highlight for 24 hours.</div>

    <h3>Known limitations</h3>
    <ul>
      <li>Some smaller filers release results via 10-Q only (no accompanying 8-K item 2.02). They won't appear here even after they've reported.</li>
      <li>Companies with no 8-K item 2.02 in the last 90 days are listed in the collapsed &ldquo;Dormant filers&rdquo; footer.</li>
      <li>EDGAR typically accepts an 8-K filing 5&ndash;15 minutes after the wire press release goes out. So even with 15-minute polling, the page can be ~30 minutes behind the wire in worst case.</li>
    </ul>

    <h2>Upcoming earnings reports</h2>

    <h3>What it shows</h3>
    <p>Confirmed upcoming earnings releases and conference calls &mdash; the dates companies have publicly announced via save-the-date press releases. Each row shows the expected release date and (when known) the conference call date and time.</p>

    <h3>Where the data comes from</h3>
    <p>Two sources, merged:</p>
    <ul>
      <li><strong>Yahoo Finance API (auto).</strong> Polls the public news feed for each tracked company. Keeps only items from wire publishers (Business Wire, GlobeNewswire, PR Newswire, ACCESS Newswire) with earnings keywords in the title. For each match, tries to extract the future date from the title; if not there, fetches the article body and parses release/call/time from the press release text.</li>
      <li><strong>Manual override</strong> (<code>earnings_data/upcoming_dates.json</code>). User-editable file for any dates we know about that the automation missed. Manually entered dates are preserved across runs.</li>
    </ul>

    <h3>How often it updates</h3>
    <p>Once a day, weekdays, at <strong>6:00 PM ET</strong>. By then most save-the-date press releases for the day have been published.</p>

    <h3>Known limitations</h3>
    <ul>
      <li><strong>Yahoo's recency limit.</strong> Yahoo Finance's news API returns only the ~10 most recent items per company. For active names with lots of analyst commentary (e.g., Five Below), a save-the-date press release can age out of the feed within a week. The manual override file catches those.</li>
      <li>Past entries are automatically cleaned out: once both the release date and call date are in the past, the row disappears from this view.</li>
    </ul>

    <h2>Companies tracked</h2>
    <p>~100 public companies headquartered in the 8-county Philadelphia region: <strong>Philadelphia</strong> + Bucks, Chester, Delaware, Montgomery (PA) + Camden, Burlington, Gloucester (NJ).</p>

    <h3>Not tracked</h3>
    <ul>
      <li><strong>Independence Blue Cross</strong> &mdash; mutual benefit corp, not a SEC filer.</li>
      <li><strong>Foreign filers</strong> like AstraZeneca &mdash; they file annual 20-F, not quarterly 10-Q/8-K.</li>
      <li><strong>Closed-end funds and ETFs</strong> &mdash; they file N-CSR/N-PORT, not quarterly earnings.</li>
    </ul>

  </div>
</div></body></html>"""

COMMON_STYLES = """
<style>
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Helvetica, Arial, sans-serif; margin: 0; background: #f4f5f7; color: #1a1a2e; }
.container { max-width: 1300px; margin: 24px auto; padding: 0 24px; }
h1 { font-size: 24px; margin: 0 0 4px; }
.meta { color: #6c757d; font-size: 13px; margin-bottom: 14px; }

.tabs { display: flex; gap: 4px; margin-bottom: 16px; border-bottom: 1px solid #d3d9de; }
.tabs a { padding: 9px 14px; text-decoration: none; color: #495057; font-size: 13.5px; font-weight: 500; border-bottom: 3px solid transparent; }
.tabs a.active { color: #1a1a2e; border-bottom-color: #1a1a2e; }
.tabs a:hover { background: #eef0f3; }

.stats { display: flex; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }
.stat { background: white; padding: 8px 14px; border-radius: 4px; font-size: 12.5px; box-shadow: 0 1px 2px rgba(0,0,0,0.06); }
.stat b { color: #1a1a2e; font-size: 15px; margin-right: 4px; }
.stat.new-stat { background: #fff4d6; border-left: 3px solid #d63031; }
.stat.new-stat b { color: #d63031; }

table { width: 100%; border-collapse: collapse; background: white; box-shadow: 0 1px 4px rgba(0,0,0,0.08); border-radius: 4px; overflow: hidden; }
th { background: #1a1a2e; color: white; text-align: left; padding: 9px 12px; font-weight: 600; font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.4px; }
td { padding: 10px 12px; border-bottom: 1px solid #ececef; vertical-align: top; font-size: 13.5px; }
tr:hover { background: #fbfbfc; }

.tk { font-family: ui-monospace, monospace; font-weight: 700; color: #2c5282; white-space: nowrap; }
.nm { font-weight: 500; min-width: 200px; }
.date { white-space: nowrap; min-width: 120px; }
.dim { color: #999; font-size: 11px; }
.rel-info { color: #2d3436; line-height: 1.55; }
.links { white-space: nowrap; font-size: 12.5px; }
.links a { color: #2c5282; text-decoration: none; }
.links a:hover { text-decoration: underline; }

.when { white-space: nowrap; font-size: 12.5px; }
.overdue { color: #d63031; font-weight: 600; }
.today { color: #d63031; font-weight: 700; background: #ffeaa7; padding: 2px 6px; border-radius: 3px; }
.soon { color: #e17055; font-weight: 700; }

.empty { color: #6c757d; font-style: italic; padding: 30px; background: white; border-radius: 4px; text-align: center; }

tr.row-new { background: #fff4d6; }
tr.row-new:hover { background: #ffeeb9; }
tr.row-new td { border-bottom: 1px solid #ffd966; }
.badge-new { display: inline-block; padding: 2px 7px; background: #d63031; color: white; border-radius: 3px; font-size: 10px; font-weight: 700; letter-spacing: 0.4px; margin-right: 6px; }

.dormant { margin-top: 30px; background: white; padding: 14px 18px; border-radius: 6px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
.dormant summary { cursor: pointer; font-weight: 600; color: #495057; font-size: 13px; }
.dormant summary:hover { color: #1a1a2e; }
.dormant-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 4px 14px; margin-top: 8px; }
.dormant-item { font-size: 12px; color: #495057; padding: 3px 0; border-bottom: 1px dotted #ececef; }
.dormant-item b { color: #2c5282; font-family: ui-monospace, monospace; }
</style>"""

# =================== Write files ===================

(HERE / "recent_earnings.html").write_text(inject_auth(build_recent_page()))
(HERE / "upcoming_earnings.html").write_text(inject_auth(build_upcoming_page()))
(HERE / "earnings_notes.html").write_text(inject_auth(build_notes_page()))

# Old URL → recent
(HERE / "earnings_dashboard.html").write_text(inject_auth(
    '<!DOCTYPE html><html><head><meta charset="utf-8"><meta http-equiv="refresh" content="0; url=recent_earnings.html">'
    '<title>Earnings</title></head><body><p>Redirecting...</p></body></html>'
))

recent_rows = build_recent_rows()
upcoming_rows = build_upcoming_rows()
print(f"Recent:   {len(recent_rows)} companies filed 8-K item 2.02 in last 90 days")
print(f"Upcoming: {len(upcoming_rows)} confirmed dates")
