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
  python3 simple_earnings.py init             # build baseline from EDGAR cache
  python3 simple_earnings.py poll             # poll Yahoo for new save-the-dates / press
  python3 simple_earnings.py edgar-poll       # refetch EDGAR submissions, detect new 8-K item 2.02 filings
  python3 simple_earnings.py edgar-firehose   # scan EDGAR Atom feeds for near-real-time detection
  python3 simple_earnings.py check-10q        # check EDGAR for 10-Q filings for any pending company
  python3 simple_earnings.py poll --live      # also send emails
"""
import json, os, re, sys, subprocess, time
import xml.etree.ElementTree as XMLTree
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).parent
ED_DIR = HERE / "earnings_data"
COMPANIES_FILE = ED_DIR / "expanded_companies.json"
SUBMISSIONS_CACHE = ED_DIR / "submissions_cache"
CACHE_FILE = ED_DIR / "cache.json"
OVERRIDES_FILE = ED_DIR / "manual_overrides.json"
LOG_FILE = ED_DIR / "simple_earnings_log.txt"
FIREHOSE_STATE_FILE = ED_DIR / "firehose_state.json"

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

from email_utils import (send_email, subject_new_report, subject_new_report_701, subject_10q_filed,
                         body_new_report_edgar, body_new_report_edgar_701, body_new_report_10q,
                         body_new_report_wire)

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

def find_latest_earnings_filing(sub):
    """Find the most recent earnings-related filing across three types:
      8k_202 — 8-K item 2.02 (standard earnings press release)
      8k_701 — 8-K item 7.01+9.01 (Reg FD earnings disclosure, used by some smaller companies)
      10q    — bare 10-Q (no accompanying 8-K press release)
    Returns the most recent of all found, with a 'filing_type' key, or None."""
    rec = sub.get("filings", {}).get("recent", {}) or {}
    forms = rec.get("form", [])
    items_list = rec.get("items", [])
    dates = rec.get("filingDate", [])
    acceptance = rec.get("acceptanceDateTime", [])
    accessions = rec.get("accessionNumber", [])
    docs = rec.get("primaryDocument", [])
    cutoff = (datetime.now(timezone.utc) - timedelta(days=180)).strftime("%Y-%m-%d")
    best_202 = best_701 = best_10q = None
    for i in range(len(forms)):
        if dates[i] < cutoff: break
        f = forms[i]
        items = (items_list[i] if i < len(items_list) else "") or ""
        acc_dt = acceptance[i] if i < len(acceptance) else ""
        if f == "8-K":
            if "2.02" in items and best_202 is None:
                best_202 = {"filing_type": "8k_202", "date": acc_dt, "filing_date": dates[i],
                            "accession": accessions[i], "primary_doc": docs[i], "items": items}
            elif "7.01" in items and "9.01" in items and best_701 is None:
                best_701 = {"filing_type": "8k_701", "date": acc_dt, "filing_date": dates[i],
                            "accession": accessions[i], "primary_doc": docs[i], "items": items}
        elif f == "10-Q" and best_10q is None:
            best_10q = {"filing_type": "10q", "date": acc_dt, "filing_date": dates[i],
                        "accession": accessions[i], "primary_doc": docs[i], "items": ""}
        if best_202 and best_701 and best_10q: break
    candidates = [x for x in [best_202, best_701, best_10q] if x]
    if not candidates: return None
    return max(candidates, key=lambda x: x["filing_date"])

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

def process_new_filing(ticker, info, filing, url, live, now):
    """Update cache entry and send typed email alert for a newly detected EDGAR filing.

    Parameters
    ----------
    ticker  : str  — ticker symbol (cache key)
    info    : dict — mutable cache entry for this ticker (modified in place)
    filing  : dict — filing dict with keys: filing_type, accession, filing_date, date, primary_doc
    url     : str  — direct URL to the filing document (or filing index page from Atom)
    live    : bool — if True, send email; if False, log what would be sent
    now     : str  — ISO timestamp for detected_at bookkeeping
    """
    ft = filing["filing_type"]  # "8k_202", "8k_701", or "10q"
    # Canonical detected fields (any type)
    info["last_detected_date"] = filing["filing_date"]
    info["last_detected_accession"] = filing["accession"]
    info["last_detected_type"] = ft
    info["last_detected_url"] = url
    info["last_detected_at"] = now
    # 8-K-specific fields + 10-Q watch (only for press-release types)
    if ft in ("8k_202", "8k_701"):
        info["last_8k_date"] = filing["filing_date"]
        info["last_8k_accession"] = filing["accession"]
        info["last_8k_url"] = url
        info["last_8k_detected_at"] = now
        info["pending_10q_since"] = filing["filing_date"]
        info["last_10q_url"] = None
    # Update last_event display anchor
    if filing["date"] > info.get("last_event_date", ""):
        info["last_event_date"] = filing["date"]
        info["last_event_title"] = f"{ft} filed {filing['filing_date']}"
        info["last_event_source"] = "edgar"
        info["last_event_url"] = url
    log(f"  ★ NEW {ft} for {ticker} ({info.get('name','')[:40]}) filed {filing['filing_date']}")
    log(f"    {url}")
    name = info.get("name", ticker)
    if ft == "8k_202":
        subj = subject_new_report(name, ticker)
        body = body_new_report_edgar(name, ticker, filing["filing_date"], url,
                   accepted_at=filing["date"], detected_at=now)
    elif ft == "8k_701":
        subj = subject_new_report_701(name, ticker)
        body = body_new_report_edgar_701(name, ticker, filing["filing_date"], url,
                   accepted_at=filing["date"], detected_at=now)
    else:  # 10q
        subj = subject_10q_filed(name, ticker)
        body = body_new_report_10q(name, ticker, filing["filing_date"], url,
                   accepted_at=filing["date"], detected_at=now)
    if live:
        try:
            send_email(subj, body, log_fn=log)
            log(f"    ✉ alert sent ({ft})")
        except Exception as e:
            log(f"    ⚠ email err: {e}")
    else:
        log(f"    [no-email] would send: {subj}")


def edgar_poll(live=False):
    """For each tracked company, fetch fresh EDGAR submissions. Detect new filings of any
    earnings type (8-K 2.02, 8-K 7.01+9.01, or bare 10-Q). Update cache and send typed alert."""
    log(f"=== EDGAR POLL: scanning for new earnings filings (live={live}) ===")
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
        latest = find_latest_earnings_filing(sub)
        if not latest: continue
        # Compare against last detected filing of any type (fall back to 8k accession for legacy entries)
        prev_accession = info.get("last_detected_accession") or info.get("last_8k_accession")
        if latest["accession"] == prev_accession:
            continue  # nothing new
        # NEW FILING DETECTED — build URL then delegate to shared helper
        cik_no_pad = int(cik)
        acc_no_clean = latest["accession"].replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{cik_no_pad}/{acc_no_clean}/{latest['primary_doc']}"
        process_new_filing(ticker, info, latest, url, live, now)
        # Update submissions cache on disk
        cf = SUBMISSIONS_CACHE / f"CIK{cik_padded(cik)}.json"
        try: cf.write_text(json.dumps(sub))
        except Exception as e: log(f"  cache write err: {e}")
        new_count += 1
    CACHE_FILE.write_text(json.dumps(cache, indent=1))
    log(f"=== EDGAR POLL complete. Fetched {fetched} companies, {new_count} new filings. ===")

# ============ EDGAR FIREHOSE ============

def _fetch_atom_feed(feed_type, start=0):
    """Fetch an EDGAR 'latest filings' Atom feed. feed_type is '8-K' or '10-Q'.
    Returns raw XML bytes or None on failure."""
    url = (f"https://www.sec.gov/cgi-bin/browse-edgar"
           f"?action=getcurrent&type={feed_type}&count=100&output=atom&start={start}")
    try:
        out = subprocess.run(
            ["curl", "-s", "-A", UA, "-L", "--max-time", "20", url],
            capture_output=True, timeout=25,
        )
        if out.returncode == 0 and out.stdout:
            return out.stdout
        log(f"  firehose: {feed_type} feed fetch failed (rc={out.returncode})")
        return None
    except Exception as e:
        log(f"  firehose: {feed_type} feed fetch err: {e}")
        return None


def _parse_atom_entries(xml_bytes):
    """Parse an Atom feed. Returns list of dicts with keys:
       cik (zero-padded 10-digit str), accession (with dashes), form, items,
       updated (ISO str), link (str), filing_date (YYYY-MM-DD str), title (str).
    """
    ATOM_NS = "http://www.w3.org/2005/Atom"
    entries = []
    try:
        root = XMLTree.fromstring(xml_bytes)
    except XMLTree.ParseError as e:
        log(f"  firehose: XML parse error: {e}")
        return entries

    for entry in root.findall(f"{{{ATOM_NS}}}entry"):
        title_el = entry.find(f"{{{ATOM_NS}}}title")
        title_text = (title_el.text or "") if title_el is not None else ""

        # CIK: extract 10-digit number from title "(0001234567) (Filer)" pattern
        cik = None
        m = re.search(r'\((\d{10})\)', title_text)
        if m:
            cik = m.group(1)  # already 10-digit from the feed
        else:
            # Fallback: try any run of digits that looks like a CIK
            m2 = re.search(r'\((\d+)\)', title_text)
            if m2:
                raw = m2.group(1)
                try:
                    cik = f"{int(raw):010d}"
                except ValueError:
                    pass
        if not cik:
            continue

        # Accession number: from <id> or <summary>
        id_el = entry.find(f"{{{ATOM_NS}}}id")
        id_text = (id_el.text or "") if id_el is not None else ""
        # EDGAR id looks like: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=...&type=8-K...
        # Accession is also in <summary> as something like "Accession Number: 0001234567-26-000123"
        summary_el = entry.find(f"{{{ATOM_NS}}}summary")
        summary_text = (summary_el.text or "") if summary_el is not None else ""

        acc_m = re.search(r'(\d{10}-\d{2}-\d{6})', summary_text + " " + id_text)
        accession = acc_m.group(1) if acc_m else ""

        # Form type from <category term="8-K"> or similar
        cat_el = entry.find(f"{{{ATOM_NS}}}category")
        form = ""
        if cat_el is not None:
            form = cat_el.get("term", "") or cat_el.get("label", "")
        # Also try from title (e.g. "COMPANY NAME (0001234567) (Filer)")
        # The title contains the form type sometimes; rely on category

        # Items: from <summary> text, e.g. "Items 2.02, 9.01"
        items_m = re.findall(r'Item\s+(\d+\.\d+)', summary_text, re.IGNORECASE)
        items = ", ".join(items_m)

        # Updated timestamp
        updated_el = entry.find(f"{{{ATOM_NS}}}updated")
        updated = (updated_el.text or "") if updated_el is not None else ""

        # Filing date: derive from updated (YYYY-MM-DD portion), good enough
        filing_date = updated[:10] if updated else ""

        # Link: <link href="...">
        link_el = entry.find(f"{{{ATOM_NS}}}link")
        link = ""
        if link_el is not None:
            link = link_el.get("href", "")

        entries.append({
            "cik": cik,
            "accession": accession,
            "form": form,
            "items": items,
            "updated": updated,
            "filing_date": filing_date,
            "link": link,
            "title": title_text,
        })
    return entries


def _classify_atom_entry(entry):
    """Classify an Atom entry exactly like find_latest_earnings_filing.
    Returns 'skip', '8k_202', '8k_701', or '10q'."""
    form = entry.get("form", "")
    items = entry.get("items", "")
    # Exact form matches — mirrors find_latest_earnings_filing, which ignores
    # amended filings (8-K/A, 10-Q/A) so the firehose can't alert on them either
    if form == "8-K":
        if "2.02" in items:
            return "8k_202"
        if "7.01" in items and "9.01" in items:
            return "8k_701"
        return "skip"
    if form == "10-Q":
        return "10q"
    return "skip"


def edgar_firehose(live=False):
    """Scan EDGAR latest-filings Atom feeds for near-real-time detection (~2 min lag).
    Classifies 8-K and 10-Q entries exactly like edgar-poll. Skips entries already
    in cache (accession dedup). Updates cache and sends typed alerts via process_new_filing."""
    log(f"=== EDGAR FIREHOSE: scanning Atom feeds (live={live}) ===")
    if not CACHE_FILE.exists():
        log("  ⚠ cache.json missing — run `init` first")
        return

    cache = json.loads(CACHE_FILE.read_text())
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    # Build watched map: zero-padded 10-digit CIK → ticker
    watched = {}
    for ticker, info in cache.items():
        cik_raw = info.get("cik")
        if not cik_raw:
            continue
        try:
            padded = f"{int(cik_raw):010d}"
        except (ValueError, TypeError):
            continue
        watched[padded] = ticker

    # Load watermark state
    firehose_state = {}
    if FIREHOSE_STATE_FILE.exists():
        try:
            firehose_state = json.loads(FIREHOSE_STATE_FILE.read_text())
        except Exception:
            pass
    last_seen_str = firehose_state.get("last_updated_seen", "")
    if last_seen_str:
        try:
            last_seen_dt = datetime.fromisoformat(last_seen_str)
            if last_seen_dt.tzinfo is None:
                last_seen_dt = last_seen_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            last_seen_dt = datetime.min.replace(tzinfo=timezone.utc)
    else:
        last_seen_dt = datetime.min.replace(tzinfo=timezone.utc)

    # 10-minute overlap to avoid gaps at boundary (skip if watermark is the epoch sentinel)
    if last_seen_dt == datetime.min.replace(tzinfo=timezone.utc):
        cutoff_dt = last_seen_dt
    else:
        cutoff_dt = last_seen_dt - timedelta(minutes=10)

    def _parse_updated(s):
        if not s:
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)

    all_entries = []
    max_updated = last_seen_dt  # will track new maximum

    for feed_type in ("8-K", "10-Q"):
        xml_bytes = _fetch_atom_feed(feed_type)
        if xml_bytes is None:
            continue
        entries = _parse_atom_entries(xml_bytes)
        log(f"  firehose: {feed_type} feed — {len(entries)} entries parsed")

        # If EVERY entry on page 1 is newer than the watermark, fetch page 2 as well
        if entries and all(_parse_updated(e["updated"]) > last_seen_dt for e in entries):
            log(f"  firehose: all {feed_type} entries newer than watermark — fetching page 2")
            xml2 = _fetch_atom_feed(feed_type, start=100)
            if xml2:
                page2 = _parse_atom_entries(xml2)
                log(f"  firehose: {feed_type} page 2 — {len(page2)} entries")
                entries.extend(page2)

        all_entries.extend(entries)

    log(f"  firehose: {len(watched)} watched CIKs, {len(all_entries)} total feed entries")

    new_detections = 0
    for entry in all_entries:
        entry_dt = _parse_updated(entry["updated"])

        # Skip entries older than cutoff (with overlap)
        if entry_dt <= cutoff_dt:
            continue

        # Track max updated seen this run
        if entry_dt > max_updated:
            max_updated = entry_dt

        # Skip if not a watched company
        cik = entry["cik"]
        if cik not in watched:
            continue

        ticker = watched[cik]
        info = cache[ticker]

        # Classify
        filing_type = _classify_atom_entry(entry)
        if filing_type == "skip":
            continue

        # Accession dedup — shared key with edgar-poll prevents double-email
        prev_accession = info.get("last_detected_accession") or info.get("last_8k_accession")
        if entry["accession"] and entry["accession"] == prev_accession:
            continue  # already known

        # NEW match — try EDGAR submissions API to get primary doc URL
        sub = fetch_edgar_submissions(cik)
        time.sleep(0.12)

        filing = None
        url = entry["link"]  # fallback: filing index page from Atom

        if sub:
            # Try to find this exact accession in the live submissions
            rec = sub.get("filings", {}).get("recent", {}) or {}
            accessions_live = rec.get("accessionNumber", [])
            acc_formatted = entry["accession"]  # with dashes
            if acc_formatted in accessions_live:
                idx = accessions_live.index(acc_formatted)
                docs = rec.get("primaryDocument", [])
                filing_dates = rec.get("filingDate", [])
                acceptance = rec.get("acceptanceDateTime", [])
                primary_doc = docs[idx] if idx < len(docs) else ""
                fd = filing_dates[idx] if idx < len(filing_dates) else entry["filing_date"]
                acc_dt = acceptance[idx] if idx < len(acceptance) else entry["updated"]
                if primary_doc:
                    cik_no_pad = int(cik)
                    acc_no_clean = acc_formatted.replace("-", "")
                    url = f"https://www.sec.gov/Archives/edgar/data/{cik_no_pad}/{acc_no_clean}/{primary_doc}"
                filing = {
                    "filing_type": filing_type,
                    "accession": acc_formatted,
                    "filing_date": fd,
                    "date": acc_dt,
                    "primary_doc": primary_doc,
                }

        if filing is None:
            # Not yet indexed in submissions API — build from Atom entry
            filing = {
                "filing_type": filing_type,
                "accession": entry["accession"],
                "filing_date": entry["filing_date"],
                "date": entry["updated"],
                "primary_doc": "",
            }

        process_new_filing(ticker, info, filing, url, live, now_iso)
        new_detections += 1

    # Save updated cache
    CACHE_FILE.write_text(json.dumps(cache, indent=1))

    # Write watermark
    firehose_state["last_updated_seen"] = max_updated.isoformat() if max_updated != datetime.min.replace(tzinfo=timezone.utc) else ""
    firehose_state["last_run"] = now_iso
    FIREHOSE_STATE_FILE.write_text(json.dumps(firehose_state, indent=1))

    log(f"=== EDGAR FIREHOSE complete. {new_detections} new detections. ===")


# ============ 10-Q WATCH ============

def find_latest_10q(sub, since_date):
    """Walk submissions, return most recent 10-Q with filingDate >= since_date, or None."""
    rec = sub.get("filings", {}).get("recent", {}) or {}
    forms = rec.get("form", [])
    dates = rec.get("filingDate", [])
    accessions = rec.get("accessionNumber", [])
    docs = rec.get("primaryDocument", [])
    for i in range(len(forms)):
        if forms[i] != "10-Q": continue
        if dates[i] < since_date: break  # sorted newest-first
        return {"filing_date": dates[i], "accession": accessions[i], "primary_doc": docs[i]}
    return None

def check_pending_10qs():
    """For each company with pending_10q_since set, check EDGAR for a matching 10-Q.
    If found, store last_10q_url and clear the pending flag. Auto-clears flags older than 7 days."""
    log("=== CHECK-10Q: scanning for pending 10-Q filings ===")
    if not CACHE_FILE.exists():
        log("  ⚠ cache.json missing — run `init` first")
        return
    cache = json.loads(CACHE_FILE.read_text())
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    checked = 0
    found = 0
    cleared_stale = 0
    for ticker, info in cache.items():
        since = info.get("pending_10q_since")
        if not since: continue
        # Auto-clear flags older than 7 days
        try:
            age = (datetime.strptime(today_str, "%Y-%m-%d") - datetime.strptime(since, "%Y-%m-%d")).days
        except Exception:
            age = 0
        if age > 7:
            log(f"  {ticker}: pending_10q_since={since} is {age}d old — clearing stale flag")
            del info["pending_10q_since"]
            cleared_stale += 1
            continue
        cik = info.get("cik")
        if not cik: continue
        sub = fetch_edgar_submissions(cik)
        time.sleep(0.12)
        checked += 1
        if not sub: continue
        q = find_latest_10q(sub, since)
        if not q: continue
        cik_no_pad = int(cik)
        acc_no_clean = q["accession"].replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{cik_no_pad}/{acc_no_clean}/{q['primary_doc']}"
        info["last_10q_url"] = url
        del info["pending_10q_since"]
        found += 1
        log(f"  ✓ 10-Q found for {ticker} ({info.get('name','')[:40]}) filed {q['filing_date']}")
        log(f"    {url}")
    CACHE_FILE.write_text(json.dumps(cache, indent=1))
    log(f"=== CHECK-10Q complete. Checked {checked}, found {found}, cleared {cleared_stale} stale. ===")

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
    elif cmd == "edgar-firehose":
        edgar_firehose(live=live)
    elif cmd == "check-10q":
        check_pending_10qs()
    else:
        print(__doc__)

if __name__ == "__main__":
    main()
