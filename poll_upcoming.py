#!/usr/bin/env python3
"""Daily Yahoo Finance poll to capture save-the-date earnings announcements.

Runs once a day at 6 PM ET via GitHub Actions cron.

Per company:
  1. Query Yahoo Finance: ?q={ticker}&newsCount=25
  2. Keep only WIRE-PUBLISHED items (Business Wire / GlobeNewswire / PR Newswire / ACCESS Newswire)
  3. Keep only titles with earnings keywords (and not analyst-noise negative phrases)
  4. For new items (UUID not previously seen):
       a. Try to pull a future date out of the title (handles 'NEXGEL To Report ... on May 15th')
       b. If title has no date, FETCH the Yahoo article body and parse it
          for 'release ... on DATE' + 'conference call ... at TIME' patterns
       c. If a FUTURE date is extracted, write it to upcoming_dates.json
  5. After all companies polled, clean past entries out of upcoming_dates.json

State: earnings_data/yahoo_upcoming_state.json — UUIDs of articles we've already processed
Output: earnings_data/upcoming_dates.json — what the Upcoming page reads
"""
import json, os, sys, subprocess, re, html, time
from datetime import datetime, date, timezone, timedelta
from pathlib import Path
from email_utils import send_email, subject_save_the_date, body_save_the_date

HERE = Path(__file__).parent
ED = HERE / "earnings_data"
COMPANIES_FILE = ED / "expanded_companies.json"
UPCOMING_FILE = ED / "upcoming_dates.json"
STATE_FILE = ED / "yahoo_upcoming_state.json"
LOG_FILE = ED / "poll_upcoming_log.txt"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"

# ============ FILTERS ============

WIRE_PUBLISHERS = {
    "Business Wire", "BusinessWire", "Businesswire",
    "GlobeNewswire", "Globe Newswire",
    "PR Newswire", "PRNewswire",
    "ACCESS Newswire",
    "CNW Group",
    "TMX Newsfile",
}

EARNINGS_KEYWORDS = {
    "earnings", "quarterly results", "quarterly earnings", "quarterly report",
    "annual results", "annual earnings", "annual report",
    "financial results", "financial report", "financial release",
    "fiscal year", "fiscal quarter",
    "earnings call", "earnings release", "earnings report", "conference call",
    "first quarter", "second quarter", "third quarter", "fourth quarter",
    "first-quarter", "second-quarter", "third-quarter", "fourth-quarter",
    "1q", "2q", "3q", "4q", "q1 ", "q2 ", "q3 ", "q4 ",
}

NEGATIVE_PHRASES = {
    "earnings preview", "earnings on the horizon", "post-earnings",
    "reports next week", "ahead of the quarter",
    "non-deal roadshow", "fireside chat", "investor day",
    "to participate in",
    # Note: "investor conference" removed — too broad; catches "investor conference call"
    # which is standard phrasing for earnings calls (e.g., J&J Q2 2026).
    # Non-earnings conference appearances lack earnings keywords and are filtered there.
}

def title_matches_earnings(title):
    t = (title or "").lower()
    if any(neg in t for neg in NEGATIVE_PHRASES): return False
    return any(kw in t for kw in EARNINGS_KEYWORDS)

# ============ LOG ============

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, file=sys.stderr)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

# ============ HTTP ============

def fetch_json(url, timeout=15):
    """Yahoo Finance API — uses curl-cffi if available, falls back to plain curl."""
    try:
        from curl_cffi import requests
        r = requests.get(url, impersonate="chrome120", timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except ImportError:
        out = subprocess.run(["curl","-s","-A",UA,"-L","--max-time",str(timeout), url],
                             capture_output=True, text=True, timeout=timeout+3)
        try: return json.loads(out.stdout)
        except: return None
    except Exception as e:
        log(f"  fetch_json err: {e}")
        return None

def fetch_html(url, timeout=20):
    """Fetch a Yahoo article URL with curl-cffi (real Chrome TLS fingerprint)."""
    try:
        from curl_cffi import requests
        r = requests.get(url, impersonate="chrome120", timeout=timeout)
        return r.text if r.status_code == 200 else ""
    except Exception as e:
        log(f"  fetch_html err: {e}")
        return ""

# ============ DATE EXTRACTION ============

MONTHS_RE = r"(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
DAYNAME_OPT = r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?,?\s*"

def normalize_date(month_name, day, year):
    try:
        m = datetime.strptime(month_name[:3].capitalize(), "%b").month
        return f"{int(year):04d}-{m:02d}-{int(day):02d}"
    except: return None

def normalize_time(time_str):
    """'4:30 p.m. Eastern Time' or '9:00am ET' → 'HH:MM ET' 24-hour-ish."""
    if not time_str: return None
    m = re.search(r"(\d{1,2}):(\d{2})\s*(a\.?m\.?|p\.?m\.?)", time_str, re.I)
    if not m: return time_str
    h = int(m.group(1)); mn = m.group(2)
    ampm = m.group(3).replace(".","").lower()
    if ampm == "pm" and h < 12: h += 12
    if ampm == "am" and h == 12: h = 0
    return f"{h:02d}:{mn} ET"

def year_disambiguate(month_num, day, publish_unix):
    """If we know the article was published on date X, a date later in the year is THIS year;
    earlier in the year is NEXT year (a 'fiscal Q1 reported in June' could mean year+1)."""
    anchor = datetime.fromtimestamp(publish_unix) if publish_unix else datetime.now()
    candidate = datetime(anchor.year, month_num, day)
    if (candidate - anchor).days < -30:
        # Date is more than a month before publication → must mean next year
        return anchor.year + 1
    return anchor.year

def extract_from_title(title, publish_unix=None):
    """Direct date in the title — e.g., 'NEXGEL To Report ... on May 15th'."""
    m = re.search(rf"({MONTHS_RE})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?", title, re.I)
    if not m: return None
    mon_name, day, year = m.group(1), int(m.group(2)), m.group(3)
    try: mn = datetime.strptime(mon_name[:3].capitalize(), "%b").month
    except: return None
    if year: yr = int(year)
    else: yr = year_disambiguate(mn, day, publish_unix)
    return {
        "release_date": f"{yr:04d}-{mn:02d}-{day:02d}",
        "call_date": None,
        "call_time": None,
    }

def extract_from_body(html_text, publish_unix=None):
    """Parse Yahoo article body for save-the-date language."""
    if not html_text: return None
    # Strip script/style first to reduce noise
    txt = re.sub(r"<script[^>]*>.*?</script>", " ", html_text, flags=re.S|re.I)
    txt = re.sub(r"<style[^>]*>.*?</style>", " ", txt, flags=re.S|re.I)
    txt = html.unescape(re.sub(r"<[^>]+>", " ", txt))
    txt = re.sub(r"\s+", " ", txt)

    result = {"release_date": None, "call_date": None, "call_time": None}

    # ----- Release date -----
    # "(will release|to release|will report) ... results|earnings ... (after market close|on) DATE"
    rel_patterns = [
        rf"(?:will\s+release|to\s+release|will\s+report|to\s+report|announce)\s+(?:[^.]{{0,80}}?)"
        rf"(?:results|earnings|financial\s+results)\s+(?:[^.]{{0,120}}?)"
        rf"(?:on|after\s+the\s+market\s+(?:closes?|opens?))\s+(?:on\s+)?"
        rf"{DAYNAME_OPT}({MONTHS_RE})\s+(\d{{1,2}}),?\s+(\d{{4}})",
        rf"(?:release|report)\s+(?:its\s+)?(?:[^.]{{0,60}}?)"
        rf"(?:results|earnings)\s+(?:[^.]{{0,80}}?)"
        rf"{DAYNAME_OPT}({MONTHS_RE})\s+(\d{{1,2}}),?\s+(\d{{4}})",
    ]
    for p in rel_patterns:
        m = re.search(p, txt, re.I)
        if m:
            result["release_date"] = normalize_date(m.group(1), m.group(2), m.group(3))
            break

    # ----- Call date + time -----
    # "conference call|earnings call|webcast ... on DATE ... at TIME"
    call_p = rf"(?:conference\s+call|earnings\s+call|webcast)\s+(?:[^.]{{0,200}}?)" \
             rf"(?:on\s+)?{DAYNAME_OPT}({MONTHS_RE})\s+(\d{{1,2}}),?\s+(\d{{4}})" \
             rf"(?:[^.]{{0,80}}?)" \
             rf"(\d{{1,2}}:\d{{2}}\s*[ap]\.?m\.?\s*(?:\([^)]*\))?\s*(?:Eastern\s*Time|ET|EDT|EST)?)"
    m = re.search(call_p, txt, re.I)
    if m:
        result["call_date"] = normalize_date(m.group(1), m.group(2), m.group(3))
        result["call_time"] = normalize_time(m.group(4))
    else:
        # Just a time near 'call' or 'webcast'
        m_time = re.search(
            rf"(?:conference\s+call|earnings\s+call|webcast)\s+(?:[^.]{{0,200}}?)"
            rf"(\d{{1,2}}:\d{{2}}\s*[ap]\.?m\.?\s*(?:\([^)]*\))?\s*(?:Eastern\s*Time|ET|EDT|EST)?)",
            txt, re.I
        )
        if m_time:
            result["call_time"] = normalize_time(m_time.group(1))

    return result if any(result.values()) else None

# ============ STATE ============

def load_state():
    if STATE_FILE.exists():
        d = json.loads(STATE_FILE.read_text())
        return {k: set(v) for k, v in d.items()}
    return {}

def save_state(state):
    STATE_FILE.write_text(json.dumps({k: sorted(v) for k, v in state.items()}, indent=1))

def load_upcoming():
    if UPCOMING_FILE.exists():
        return json.loads(UPCOMING_FILE.read_text())
    return {}

def save_upcoming(data):
    UPCOMING_FILE.write_text(json.dumps(data, indent=1))

# ============ MAIN ============

def main():
    args = sys.argv[1:]
    init_mode = "--init" in args
    live = "--live" in args
    log(f"=== Yahoo upcoming-poll start (init={init_mode}, live={live}) ===")

    companies = json.loads(COMPANIES_FILE.read_text())
    targets = [c for c in companies
               if c.get("priority_tier") in (1, 2)
               and (c.get("tickers") or [c.get("ticker_hint","")])[0]]
    log(f"Polling Yahoo for {len(targets)} companies")

    state = load_state() if not init_mode else {}
    upcoming = load_upcoming()

    new_found = 0
    today_iso = date.today().isoformat()

    for c in targets:
        ticker = (c.get("tickers") or [c.get("ticker_hint","")])[0]
        if not ticker: continue
        url = f"https://query1.finance.yahoo.com/v1/finance/search?q={ticker}&newsCount=25"
        data = fetch_json(url)
        time.sleep(0.3)  # polite to Yahoo
        if not data: continue
        items = data.get("news", [])

        seen = state.get(ticker, set())
        for item in items:
            uuid = item.get("uuid")
            if not uuid: continue
            if uuid in seen: continue
            seen.add(uuid)
            publisher = item.get("publisher", "")
            if publisher not in WIRE_PUBLISHERS: continue
            title = item.get("title", "")
            if not title_matches_earnings(title): continue

            # Try title-only date extraction first
            extracted = extract_from_title(title, item.get("providerPublishTime"))
            # If no future date in title, fetch article body
            if not extracted or not extracted.get("release_date") or extracted["release_date"] < today_iso:
                article_url = item.get("link","")
                if article_url:
                    body = fetch_html(article_url)
                    time.sleep(0.5)
                    body_dates = extract_from_body(body, item.get("providerPublishTime"))
                    if body_dates:
                        # Merge: title wins for release_date if it had one, body fills the rest
                        merged = extracted or {"release_date": None, "call_date": None, "call_time": None}
                        for k in ("release_date","call_date","call_time"):
                            if not merged.get(k) and body_dates.get(k):
                                merged[k] = body_dates[k]
                        extracted = merged

            if not extracted: continue
            # Must have at least one future date
            primary = extracted.get("release_date") or extracted.get("call_date")
            if not primary or primary < today_iso:
                continue

            # Avoid clobbering manual entries; only add if we don't already have a
            # FUTURE entry for this ticker that's at or sooner than this one.
            cur = upcoming.get(ticker)
            if cur and isinstance(cur, dict):
                cur_primary = cur.get("release_date") or cur.get("call_date") or "9"
                if cur_primary >= today_iso and cur_primary <= primary:
                    # We already have an entry that's sooner — don't replace
                    continue

            upcoming[ticker] = {
                "release_date": extracted.get("release_date"),
                "call_date": extracted.get("call_date"),
                "call_time": extracted.get("call_time"),
                "source_url": item.get("link",""),
                "source_title": title[:240],
                "source_publisher": publisher,
                "source_type": "yahoo_auto",
                "captured_at": datetime.now().isoformat(timespec="seconds"),
            }
            new_found += 1
            log(f"  + {ticker}: release={extracted.get('release_date')} call={extracted.get('call_date')} {extracted.get('call_time')}")
            log(f"    via: {title[:100]}")
            if live:
                try:
                    name = c.get("name","") or c.get("seed_name","") or ticker
                    send_email(
                        subject_save_the_date(name, ticker),
                        body_save_the_date(name, ticker,
                            extracted.get("release_date"), extracted.get("call_date"),
                            extracted.get("call_time"), item.get("link",""), title,
                            published_unix=item.get("providerPublishTime")),
                        log_fn=log,
                    )
                    log(f"  ✉ alert sent for {ticker}")
                except Exception as e:
                    log(f"  ⚠ email err for {ticker}: {e}")

        state[ticker] = seen

    # Cleanup: drop entries where release_date AND call_date are both in the past
    removed = 0
    for tk in list(upcoming.keys()):
        if tk.startswith("_"): continue
        entry = upcoming[tk]
        if not isinstance(entry, dict): continue
        r = entry.get("release_date") or "9999"
        c = entry.get("call_date") or "9999"
        if r < today_iso and c < today_iso:
            del upcoming[tk]
            removed += 1
    if removed:
        log(f"  cleanup: removed {removed} past entries")

    save_state(state)
    save_upcoming(upcoming)
    log(f"=== Done. Captured {new_found} new save-the-dates, cleaned {removed} past. ===\n")

if __name__ == "__main__":
    main()
