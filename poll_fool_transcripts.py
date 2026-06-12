#!/usr/bin/env python3
"""Harvest transcript links for tracked companies from Motley Fool (primary)
and Yahoo Finance news search (secondary, for tickers not covered by Fool).

COPYRIGHT NOTICE: This script stores LINKS ONLY — never transcript text.
The repo is public; Fool/Insider Monkey text may not be republished here.

Primary path
    Fetch https://www.fool.com/earnings-call-transcripts/ (the ~20 most recent
    entries). For each link whose slug contains -<ticker>-q, store it.
    The index page does NOT require verifying individual transcript pages —
    we trust the links we see on the listing. Individual Fool transcript pages
    are rate-limited by Cloudflare; we never fetch them.

Secondary path
    For tickers with recent earnings activity still absent from transcripts.json,
    query Yahoo Finance news API and keep items whose title contains
    "earnings call transcript" (catches Insider Monkey / Benzinga syndications
    for companies Fool doesn't cover, like URBN and COR this cycle).

Seed backfill
    earnings_data/transcript_seeds.json — a committed file with known-good URLs
    for transcripts posted before the harvester existed or outside the index window.
    The harvester merges seeds into transcripts.json on first run.

State:  earnings_data/transcripts.json      {ticker: [{url, source, title, date_str, found_at}]}
Seeds:  earnings_data/transcript_seeds.json  same schema — committed, editor-maintained
Log:    earnings_data/transcripts_log.txt
"""
import json, re, sys, subprocess, time
from datetime import datetime, date, timedelta
from pathlib import Path

HERE = Path(__file__).parent
ED = HERE / "earnings_data"
COMPANIES_FILE = ED / "expanded_companies.json"
CACHE_FILE = ED / "cache.json"
TRANSCRIPTS_FILE = ED / "transcripts.json"
SEEDS_FILE = ED / "transcript_seeds.json"
LOG_FILE = ED / "transcripts_log.txt"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"

FOOL_BASE = "https://www.fool.com"
FOOL_INDEX_URL = "https://www.fool.com/earnings-call-transcripts/"

# How far back to look in cache when deciding whether to run Yahoo secondary search
SECONDARY_LOOKBACK_DAYS = 14

# Title keywords that indicate a full call transcript (not analyst preview/summary)
TRANSCRIPT_TITLE_KEYWORDS = ["earnings call transcript", "earnings transcript"]


# ============ LOG ============

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, file=sys.stderr)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# ============ HTTP ============

def fetch_html(url, timeout=20):
    """Fetch HTML. Uses curl-cffi (Chrome TLS fingerprint) if available, else plain curl."""
    try:
        from curl_cffi import requests as cffi_req
        r = cffi_req.get(url, impersonate="chrome120", timeout=timeout)
        if r.status_code == 200:
            return r.text
        log(f"  fetch_html {url}: HTTP {r.status_code}")
        return ""
    except ImportError:
        out = subprocess.run(
            ["curl", "-s", "-A", UA, "-L", "--max-time", str(timeout), url],
            capture_output=True, text=True, timeout=timeout + 5
        )
        return out.stdout if out.returncode == 0 else ""
    except Exception as e:
        log(f"  fetch_html err ({url}): {e}")
        return ""


def fetch_json(url, timeout=15):
    """Yahoo Finance API — curl-cffi first, fallback to plain curl."""
    try:
        from curl_cffi import requests as cffi_req
        r = cffi_req.get(url, impersonate="chrome120", timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except ImportError:
        out = subprocess.run(
            ["curl", "-s", "-A", UA, "-L", "--max-time", str(timeout), url],
            capture_output=True, text=True, timeout=timeout + 3
        )
        try:
            return json.loads(out.stdout)
        except Exception:
            return None
    except Exception as e:
        log(f"  fetch_json err: {e}")
        return None


# ============ FOOL PARSING ============

# Matches: /earnings/call-transcripts/YYYY/MM/DD/<slug>/
TRANSCRIPT_HREF_RE = re.compile(
    r"/earnings/call-transcripts/(\d{4}/\d{2}/\d{2})/([^/\s\"']+)/",
    re.I
)

_TICKER_SLUG_RE_CACHE = {}


def ticker_in_slug(slug, ticker):
    """True if -<ticker>-q appears in the slug (word-bounded).
    Requires the pattern -ticker-q to avoid false hits on single-letter tickers
    like 'A', 'IT'. E.g. 'campbells-cpb-q3-2026...' matches CPB but not 'CP'.
    """
    t = ticker.lower()
    if t not in _TICKER_SLUG_RE_CACHE:
        # Require ticker to be preceded by a dash and followed by a dash+digit(s)
        _TICKER_SLUG_RE_CACHE[t] = re.compile(r"(?:^|-)" + re.escape(t) + r"-\d", re.I)
    return bool(_TICKER_SLUG_RE_CACHE[t].search(slug))


def parse_fool_index(html_text):
    """Extract transcript entries from Fool index page.
    Returns list of {href, date_str, slug}.
    """
    results = []
    seen = set()
    for m in TRANSCRIPT_HREF_RE.finditer(html_text):
        href = f"/earnings/call-transcripts/{m.group(1)}/{m.group(2)}/"
        if href in seen:
            continue
        seen.add(href)
        year_mo_dy = m.group(1)  # e.g. "2026/06/08"
        date_str = year_mo_dy.replace("/", "-")
        results.append({
            "href": href,
            "date_str": date_str,
            "slug": m.group(2),
        })
    return results


def slug_to_title(slug, ticker):
    """Make a human-readable title from a Fool slug.
    E.g. 'campbells-cpb-q3-2026-earnings-transcript' → 'Campbell'S Cpb Q3 2026 Earnings Transcript'
    """
    parts = slug.replace("-", " ").split()
    title_parts = []
    for p in parts:
        if p.lower() == ticker.lower():
            title_parts.append(ticker.upper())
        else:
            title_parts.append(p.title())
    return " ".join(title_parts) + " | The Motley Fool"


# ============ YAHOO SECONDARY ============

def yahoo_secondary(ticker):
    """Query Yahoo Finance news API; return items that are full call transcripts.
    Returns list of {url, title, publisher}.
    """
    api_url = (
        f"https://query1.finance.yahoo.com/v1/finance/search"
        f"?q={ticker}&newsCount=25"
    )
    data = fetch_json(api_url)
    time.sleep(0.3)
    if not data:
        return []
    results = []
    for item in data.get("news", []):
        title = item.get("title", "")
        if any(kw in title.lower() for kw in TRANSCRIPT_TITLE_KEYWORDS):
            url = item.get("link", "")
            if url:
                results.append({
                    "url": url,
                    "title": title,
                    "publisher": item.get("publisher", "Yahoo Finance"),
                })
    return results


# ============ STATE ============

def load_transcripts():
    if TRANSCRIPTS_FILE.exists():
        return json.loads(TRANSCRIPTS_FILE.read_text())
    return {}


def save_transcripts(data):
    TRANSCRIPTS_FILE.write_text(json.dumps(data, indent=1, sort_keys=True))


def load_seeds():
    if SEEDS_FILE.exists():
        return json.loads(SEEDS_FILE.read_text())
    return {}


def merge_seeds(transcripts, seeds):
    """Add seed entries to transcripts.json if not already present."""
    added = 0
    for ticker, entries in seeds.items():
        existing_urls = {r["url"] for r in transcripts.get(ticker, [])}
        for entry in entries:
            if entry.get("url") and entry["url"] not in existing_urls:
                transcripts.setdefault(ticker, []).insert(0, entry)
                existing_urls.add(entry["url"])
                added += 1
    return added


# ============ MAIN ============

def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args

    log(f"=== Fool transcript harvest start (dry_run={dry_run}) ===")

    companies = json.loads(COMPANIES_FILE.read_text())
    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}

    all_tickers = []
    for c in companies:
        tickers_list = c.get("tickers") or [c.get("ticker_hint", "")]
        for t in tickers_list:
            if t:
                all_tickers.append(t)
    log(f"Loaded {len(all_tickers)} tickers from expanded_companies.json")

    today_date = date.today()
    secondary_cutoff = (today_date - timedelta(days=SECONDARY_LOOKBACK_DAYS)).isoformat()

    # Tickers with recent filings — candidates for Yahoo secondary
    recent_tickers = set()
    for t in all_tickers:
        last = cache.get(t, {}).get("last_detected_date") or ""
        if last >= secondary_cutoff:
            recent_tickers.add(t)
    log(f"Recent tickers (last filing within {SECONDARY_LOOKBACK_DAYS}d): {sorted(recent_tickers)}")

    # Load state
    transcripts = load_transcripts()
    new_found = 0

    # ---- Seed backfill ----
    seeds = load_seeds()
    if seeds:
        seed_added = merge_seeds(transcripts, seeds)
        if seed_added > 0:
            log(f"Seed backfill: added {seed_added} entries from transcript_seeds.json")
            if not dry_run:
                save_transcripts(transcripts)
        else:
            log("Seed backfill: all seeds already in state")

    # ---- PRIMARY: Fool index page ----
    log(f"Fetching Fool transcript index: {FOOL_INDEX_URL}")
    index_html = fetch_html(FOOL_INDEX_URL)
    if not index_html:
        log("  WARNING: Could not fetch Fool index (rate-limited or network error)")
        entries = []
    else:
        entries = parse_fool_index(index_html)
        log(f"  Found {len(entries)} transcript links on index page")

    index_matched = set()
    for entry in entries:
        slug = entry["slug"]
        for ticker in all_tickers:
            if ticker_in_slug(slug, ticker):
                url = FOOL_BASE + entry["href"]
                title = slug_to_title(slug, ticker)
                record = {
                    "url": url,
                    "source": "Motley Fool",
                    "title": title,
                    "date_str": entry["date_str"],
                    "found_at": datetime.now().isoformat(timespec="seconds"),
                }
                existing = transcripts.get(ticker, [])
                if url not in {r["url"] for r in existing}:
                    if not dry_run:
                        transcripts[ticker] = [record] + existing
                    new_found += 1
                    log(f"  NEW [Fool/index] {ticker}: {url}")
                    index_matched.add(ticker)
                else:
                    log(f"  SEEN [Fool/index] {ticker}: {url}")
                    index_matched.add(ticker)
                break  # one ticker per transcript link

    log(f"  Fool index matched tickers: {sorted(index_matched)}")

    # ---- SECONDARY: Yahoo Finance news search ----
    # Run for recent tickers that have no transcript yet (not in index, not in seeds)
    need_secondary = recent_tickers - index_matched - set(transcripts.keys())
    # Also run for recent tickers that do have state but oldest entry is stale
    for t in list(recent_tickers - index_matched):
        if t in transcripts:
            most_recent = transcripts[t][0].get("date_str", "0000-00-00")
            if most_recent < secondary_cutoff:
                need_secondary.add(t)

    log(f"Yahoo secondary search for {len(need_secondary)} tickers: {sorted(need_secondary)}")
    for ticker in sorted(need_secondary):
        items = yahoo_secondary(ticker)
        if items:
            for item in items:
                existing_urls = {r["url"] for r in transcripts.get(ticker, [])}
                if item["url"] not in existing_urls:
                    record = {
                        "url": item["url"],
                        "source": item["publisher"],
                        "title": item["title"],
                        "date_str": today_date.isoformat(),
                        "found_at": datetime.now().isoformat(timespec="seconds"),
                    }
                    if not dry_run:
                        transcripts.setdefault(ticker, []).insert(0, record)
                    new_found += 1
                    log(f"  NEW [Yahoo] {ticker}: {item['url']} ({item['publisher']})")
        else:
            log(f"  MISS [Yahoo] {ticker}: no transcript items in feed")

    # ---- Save ----
    if not dry_run and new_found > 0:
        save_transcripts(transcripts)
        log(f"Saved transcripts.json ({len(transcripts)} tickers with entries)")
    elif dry_run:
        log(f"DRY RUN — not writing to disk")
    else:
        log("No new transcripts found; transcripts.json unchanged")

    log(f"=== Done. {new_found} new transcript links found. ===\n")

    # Summary for CI verification
    print(f"Transcripts found: {new_found} new")
    print(f"Tickers with transcripts: {sorted(transcripts.keys())}")
    for t in ["CPB", "URBN", "FIVE", "COR", "TOL"]:
        entries_for_t = transcripts.get(t, [])
        if entries_for_t:
            e = entries_for_t[0]
            print(f"  {t}: {e['url']} ({e['source']})")
        else:
            print(f"  {t}: NOT FOUND")


if __name__ == "__main__":
    main()
