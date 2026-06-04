#!/usr/bin/env python3
"""Simple earnings tracker — EDGAR baseline + Yahoo Finance polling.

DESIGN (as agreed with Av on 2026-06-01):

Baseline:
  For each company with a CIK, find the most recent 8-K item 2.02 in our local
  EDGAR submissions cache. That's the anchor: "the most recent earnings event
  we know about for this company."

Ongoing detection:
  Poll Yahoo Finance's public news API. Keep only items that are:
    (a) from a known wire publisher (Business Wire / GlobeNewswire / PR Newswire / etc.)
    (b) have an earnings-language keyword in the title
  If any matching item is newer than the cached event for that company,
  that's a new event → log/alert + update the cache.

No classifier of "save-the-date vs earnings release" — both produce a Yahoo wire
article with earnings keywords. The next NEW such article (regardless of subtype)
is the next event in the company's cycle.

State: cache.json (single file, all companies, all tracked events).

Usage:
  python3 simple_earnings.py init           # build baseline from EDGAR cache
  python3 simple_earnings.py poll           # poll Yahoo for new save-the-dates / press
  python3 simple_earnings.py edgar-poll     # refetch EDGAR submissions, detect new 8-K item 2.02 filings
  python3 simple_earnings.py poll --live    # also send emails
"""
import json, os, sys, subprocess, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).parent
ED_DIR = HERE / "earnings_data"
COMPANIES_FILE = ED_DIR / "expanded_companies.json"
SUBMISSIONS_CACHE = ED_DIR / "submissions_cache"
CACHE_FILE = ED_DIR / "cache.json"
OVERRIDES_FILE = ED_DIR / "manual_overrides.json"
LOG_FILE = ED_DIR / "simple_earnings_log.txt"

UA = "Inquirer Newsroom agutman@inquirer.com"
ET = timezone(timedelta(hours=-4))  # EDT (June)

# ============ FILTERS (single source of truth) ============

WIRE_PUBLISHERS = {
    "Business Wire", "BusinessWire", "Businesswire",
    "GlobeNewswire", "Globe Newswire",
    "PR Newswire", "PRNewswire",
    "ACCESS Newswire",
    "CNW Group",
    "TMX Newsfile",
}

EARNINGS_KEYWORDS = {
    "earnings",                  # almost always relevant
    "quarterly results", "quarterly earnings", "quarterly report",
    "annual results", "annual earnings", "annual report",
    "financial results", "financial report", "financial release",
    "fiscal year", "fiscal quarter",
    "earnings call", "earnings release", "earnings report", "earnings preview", "earnings season",
    "conference call",
    "first quarter", "second quarter", "third quarter", "fourth quarter",
    "first-quarter", "second-quarter", "third-quarter", "fourth-quarter",
    "1q", "2q", "3q", "4q",
    "q1 ", "q2 ", "q3 ", "q4 ",   # trailing space — avoids matching "q1a" or similar
    "full-year results", "full year results",
}

# Items with these phrases are NOT earnings (analyst/news noise we want to skip)
NEGATIVE_PHRASES = {
    "earnings preview",          # Zacks-style analyst preview
    "earnings on the horizon",   # Zacks
    "post-earnings",             # commentary after the fact
    "reports next week",         # commentary
    "ahead of the quarter",      # commentary
    "investor conference", "non-deal roadshow", "fireside chat",
    "investor day",
}

def title_matches_earnings(title):
    t = (title or "").lower()
    if any(neg in t for neg in NEGATIVE_PHRASES):
        return False
    return any(kw in t for kw in EARNINGS_KEYWORDS)

# ============ EMAIL ============

from email_utils import send_email, subject_new_report, body_new_report_edgar, body_new_report_wire

# ============ LOGGING ============

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, file=sys.stderr)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

# ============ EDGAR BASELINE ============

def cik_padded(c): return f"{int(c):010d}"

def find_latest_earnings_8k(sub):
    """Walk recent filings, return the most recent 8-K with item 2.02, or None."""
    rec = sub.get("filings", {}).get("recent", {}) or {}
    forms = rec.get("form", [])
    items_list = rec.get("items", [])
    for i in range(len(forms)):
        if forms[i] != "8-K": continue
        items = (items_list[i] if i < len(items_list) else "") or ""
        if "2.02" not in items: continue
        return {
            "date": rec["acceptanceDateTime"][i],  # ISO with timezone
            "filing_date": rec["filingDate"][i],
            "accession": rec["accessionNumber"][i],
            "primary_doc": rec["primaryDocument"][i],
            "items": items,
        }
    return None

def edgar_baseline_for_company(c):
    cik = c.get("cik")
    if not cik: return None
    cf = SUBMISSIONS_CACHE / f"CIK{cik_padded(cik)}.json"
    if not cf.exists(): return None
    sub = json.loads(cf.read_text())
    rec = find_latest_earnings_8k(sub)
    if not rec: return None
    cik_no_pad = int(cik)
    acc_no = rec["accession"].replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{cik_no_pad}/{acc_no}/{rec['primary_doc']}"
    return {
        "last_event_date": rec["date"],
        "last_event_title": f"8-K item 2.02 filed {rec['filing_date']}",
        "last_event_source": "edgar",
        "last_event_url": url,
        "last_event_publisher": None,
        "edgar_accession": rec["accession"],
        # Last actual 8-K filing — sticks around even if cache.last_event_* gets
        # updated by a more-recent save-the-date.
        "last_8k_date": rec["filing_date"],
        "last_8k_accession": rec["accession"],
        "last_8k_url": url,
        "history": [],
    }

def init_baseline():
    """Build baseline from EDGAR + apply manual overrides.
    Preserves any existing cache entry that's newer than EDGAR's latest 8-K
    (so we don't lose Yahoo-detected events from prior polls when re-initializing)."""
    log("=== INIT: building baseline from EDGAR ===")
    companies = json.loads(COMPANIES_FILE.read_text())
    tracked = [c for c in companies if c.get("priority_tier") in (1, 2, 3) and c.get("cik")]
    log(f"Building baseline for {len(tracked)} companies")
    existing = {}
    if CACHE_FILE.exists():
        existing = json.loads(CACHE_FILE.read_text())

    cache = {}
    preserved = 0
    for c in tracked:
        ticker = (c.get("tickers") or [c.get("ticker_hint","")])[0]
        if not ticker: continue
        baseline = edgar_baseline_for_company(c)
        if baseline:
            entry = {
                "ticker": ticker,
                "name": c.get("name","") or c.get("seed_name",""),
                "cik": c.get("cik"),
                "priority_tier": c.get("priority_tier"),
                **baseline,
            }
            cache[ticker] = entry
            # If existing cache had a NEWER entry (e.g., Yahoo-detected), keep it
            prev = existing.get(ticker)
            if prev and prev.get("last_event_date","") > baseline["last_event_date"]:
                cache[ticker].update({
                    "last_event_date": prev["last_event_date"],
                    "last_event_title": prev.get("last_event_title",""),
                    "last_event_source": prev.get("last_event_source",""),
                    "last_event_url": prev.get("last_event_url",""),
                    "last_event_publisher": prev.get("last_event_publisher"),
                    "history": prev.get("history", []),
                })
                preserved += 1
        else:
            log(f"  {ticker}: no 8-K item 2.02 found in EDGAR cache (skipping)")
    if preserved:
        log(f"  preserved {preserved} newer entries from prior cache")
    # Apply manual overrides — entries explicitly maintained by the user
    apply_overrides(cache)
    CACHE_FILE.write_text(json.dumps(cache, indent=1))
    log(f"=== INIT complete: {len(cache)} companies have baseline ===")

def apply_overrides(cache):
    """Read manual_overrides.json and merge entries into cache where the override
    is newer than the current cache entry. Format same as cache.json."""
    if not OVERRIDES_FILE.exists(): return
    overrides = json.loads(OVERRIDES_FILE.read_text())
    applied = 0
    for ticker, entry in overrides.items():
        if ticker.startswith("_"): continue  # skip comment fields
        if not isinstance(entry, dict): continue
        cur = cache.get(ticker)
        ov_date = entry.get("last_event_date", "")
        cur_date = cur.get("last_event_date", "") if cur else ""
        if ov_date > cur_date:
            cache.setdefault(ticker, {}).update(entry)
            cache[ticker]["ticker"] = ticker
            applied += 1
    if applied:
        log(f"  applied {applied} manual override(s)")

# ============ EDGAR FRESH POLL ============

def fetch_edgar_submissions(cik):
    """Hit EDGAR live (not the cache). Returns dict or None."""
    url = f"https://data.sec.gov/submissions/CIK{cik_padded(cik)}.json"
    try:
        out = subprocess.run(
            ["curl", "-s", "-A", UA, "-L", "--max-time", "15", url],
            capture_output=True, text=True, timeout=18,
        )
        return json.loads(out.stdout) if out.returncode == 0 and out.stdout else None
    except Exception as e:
        log(f"  edgar fetch err for CIK {cik}: {e}")
        return None

def edgar_poll(live=False):
    """For each tracked company, fetch fresh EDGAR submissions. If there's a NEW
    8-K item 2.02 (accession we hadn't seen), update the cache and record the
    detection timestamp so the dashboard can highlight it for 24 hours."""
    log(f"=== EDGAR POLL: refetching submissions for new 8-K item 2.02 filings (live={live}) ===")
    if not CACHE_FILE.exists():
        log("  ⚠ cache.json missing — run `init` first")
        return
    cache = json.loads(CACHE_FILE.read_text())
    now = datetime.now(timezone.utc).isoformat()
    new_count = 0
    fetched = 0
    for ticker, info in cache.items():
        cik = info.get("cik")
        if not cik: continue
        sub = fetch_edgar_submissions(cik)
        time.sleep(0.12)  # SEC fair-use politeness
        if not sub: continue
        fetched += 1
        latest = find_latest_earnings_8k(sub)
        if not latest: continue
        # Compare against what we already have
        prev_accession = info.get("last_8k_accession")
        if latest["accession"] == prev_accession:
            continue  # nothing new
        # NEW FILING DETECTED
        cik_no_pad = int(cik)
        acc_no_clean = latest["accession"].replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{cik_no_pad}/{acc_no_clean}/{latest['primary_doc']}"
        info["last_8k_date"] = latest["filing_date"]
        info["last_8k_accession"] = latest["accession"]
        info["last_8k_url"] = url
        info["last_8k_detected_at"] = now
        # Also update the cache's "last_event" if EDGAR is now the most recent thing
        if latest["date"] > info.get("last_event_date",""):
            info["last_event_date"] = latest["date"]
            info["last_event_title"] = f"8-K item 2.02 filed {latest['filing_date']}"
            info["last_event_source"] = "edgar"
            info["last_event_url"] = url
        # Update the submissions cache on disk too (for derive_windows etc.)
        cf = SUBMISSIONS_CACHE / f"CIK{cik_padded(cik)}.json"
        try: cf.write_text(json.dumps(sub))
        except Exception as e: log(f"  cache write err: {e}")
        new_count += 1
        log(f"  ★ NEW 8-K item 2.02 for {ticker} ({info.get('name','')[:40]}) filed {latest['filing_date']}")
        log(f"    {url}")
        if live:
            try:
                name = info.get('name', ticker)
                send_email(
                    subject_new_report(name, ticker),
                    body_new_report_edgar(name, ticker, latest['filing_date'], url,
                        accepted_at=latest['date'], detected_at=now),
                    log_fn=log,
                )
                log(f"    ✉ alert sent")
            except Exception as e:
                log(f"    ⚠ email err: {e}")
    CACHE_FILE.write_text(json.dumps(cache, indent=1))
    log(f"=== EDGAR POLL complete. Fetched {fetched} companies, {new_count} new filings. ===")

# ============ YAHOO POLLING ============

def fetch_yahoo_news(ticker):
    """Run two queries — base ticker and ticker+earnings — to surface more items."""
    try:
        from curl_cffi import requests
    except ImportError:
        log("  ⚠ curl-cffi not installed; pip install curl-cffi")
        return []
    items = []
    seen_uuids = set()
    for q in (ticker, f"{ticker} earnings"):
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v1/finance/search?q={q.replace(' ', '+')}&newsCount=25",
                impersonate="chrome120", timeout=15,
            )
            if r.status_code != 200: continue
            for n in r.json().get("news", []):
                uuid = n.get("uuid")
                if uuid and uuid in seen_uuids: continue
                if uuid: seen_uuids.add(uuid)
                items.append(n)
        except Exception as e:
            log(f"  fetch err for {q}: {e}")
    return items

def is_relevant(item):
    """Wire publisher + earnings keyword."""
    publisher = item.get("publisher", "")
    if publisher not in WIRE_PUBLISHERS: return False
    return title_matches_earnings(item.get("title", ""))

def poll_all(live=False):
    log(f"=== POLL: checking Yahoo for new wire events (live={live}) ===")
    if not CACHE_FILE.exists():
        log("  ⚠ cache.json missing — run `init` first")
        return
    cache = json.loads(CACHE_FILE.read_text())
    new_events_total = 0
    for ticker, info in cache.items():
        items = fetch_yahoo_news(ticker)
        time.sleep(0.3)
        relevant = [x for x in items if is_relevant(x)]
        if not relevant: continue
        # Sort by publish time desc
        relevant.sort(key=lambda x: x.get("providerPublishTime", 0), reverse=True)

        last_known_date = info.get("last_event_date", "")
        # Parse to comparable timestamp
        try:
            last_known_ts = datetime.fromisoformat(last_known_date.replace("Z","+00:00")).timestamp()
        except:
            last_known_ts = 0

        new_items = [x for x in relevant if x.get("providerPublishTime", 0) > last_known_ts]
        if not new_items: continue

        # Most-recent first
        latest = new_items[0]
        latest_dt = datetime.fromtimestamp(latest["providerPublishTime"], tz=timezone.utc)
        log(f"  {ticker} ({info.get('name','')[:40]}): NEW event detected")
        log(f"    [{latest_dt.astimezone(ET).strftime('%a %b %d %I:%M %p ET')}] ({latest['publisher']}) {latest['title'][:120]}")
        log(f"    URL: {latest.get('link','')}")
        new_events_total += 1

        # Email alert
        name = info.get('name', ticker)
        subj = subject_new_report(name, ticker)
        body = body_new_report_wire(name, ticker, latest['providerPublishTime'],
            latest.get('publisher', ''), latest['title'], latest.get('link', ''))
        if live:
            try:
                send_email(subj, body, log_fn=log)
                log(f"    ✉ alert sent")
            except Exception as e:
                log(f"    ⚠ email err: {e}")
        else:
            log(f"    [no-email] would send: {subj}")

        # Update cache
        info.setdefault("history", []).append({
            "date": info.get("last_event_date"),
            "source": info.get("last_event_source"),
            "title": info.get("last_event_title"),
            "url": info.get("last_event_url"),
        })
        info["last_event_date"] = latest_dt.isoformat()
        info["last_event_title"] = latest["title"]
        info["last_event_source"] = "yahoo"
        info["last_event_url"] = latest.get("link", "")
        info["last_event_publisher"] = latest.get("publisher", "")

    CACHE_FILE.write_text(json.dumps(cache, indent=1))
    log(f"=== POLL complete: {new_events_total} new events ===")


# ============ MAIN ============

def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__); return
    cmd = args[0]
    live = "--live" in args
    if cmd == "init":
        init_baseline()
    elif cmd == "poll":
        poll_all(live=live)
    elif cmd == "edgar-poll":
        edgar_poll(live=live)
    else:
        print(__doc__)

if __name__ == "__main__":
    main()
