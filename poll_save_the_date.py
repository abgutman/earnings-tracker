#!/usr/bin/env python3
"""Save-the-date sweep — looks for upcoming earnings-call announcements.

Companies frequently pre-announce when they'll release earnings + host the call
weeks in advance. We scrape GlobeNewswire / PR Newswire for these announcements
and populate confirmed_dates.json.

The earnings-tracker dashboard reads confirmed_dates.json and surfaces these
in the "Confirmed date" column.

Designed to run Mon/Wed/Fri evenings via GitHub Actions cron.

Output: earnings_data/confirmed_dates.json
  {
    "TICKER": [
      {
        "release_date": "2026-07-22",      # when earnings drop
        "call_date":    "2026-07-23",      # when the call is
        "call_time":    "09:00 ET",        # if known
        "source_url":   "https://...",     # the announcement URL
        "captured_at":  "2026-06-01T..."
      },
      ...
    ]
  }
"""
import json, sys, subprocess, re, html, time
from datetime import datetime, date
from pathlib import Path

HERE = Path(__file__).parent
OUT_FILE = HERE / "confirmed_dates.json"
LOG_FILE = HERE / "save_the_date_log.txt"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15"

# Same wire mapping as poll_wires.py — Tier 1 only for now
TIER_1_GLOBENEWSWIRE = {
    "BDN":   ("Brandywine Realty Trust",      "brandywine-realty-trust"),
    "TOL":   ("Toll Brothers",                "toll-brothers"),
    "FIVE":  ("Five Below",                   "five-below"),
    "URBN":  ("Urban Outfitters",             "urban-outfitters"),
    "QRTEA": ("Qurate Retail Group",          "qurate-retail"),
}

# Title patterns suggesting a save-the-date (not the earnings release itself)
SAVE_THE_DATE_TITLE = re.compile(
    r"\b(announces|confirms|schedules|will release|will report|to host|"
    r"to release|conference call|webcast|announce.*conference)\b",
    re.I
)
# Must also have an earnings-related keyword
EARNINGS_KW = re.compile(
    r"\b(earnings|quarter|q[1-4]|fiscal|results|financial)\b", re.I
)

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{ts}] {msg}\n")
    print(f"[{ts}] {msg}", file=sys.stderr)

def fetch(url, timeout=15):
    try:
        out = subprocess.run(
            ["curl","-s","-A",UA,"-L","--max-redirs","5","--max-time",str(timeout), url],
            capture_output=True, text=True, timeout=timeout+3,
        )
        return out.stdout if out.returncode == 0 else ""
    except: return ""

def gnw_titles(slug):
    """List most-recent ~15 release titles + URLs for a GlobeNewswire org."""
    text = fetch(f"https://www.globenewswire.com/search/keyword/{slug}")
    if not text: return []
    rel = re.findall(r'href="(/news-release/\d{4}/\d{2}/\d{2}/[^"]+\.html)"', text)
    seen = set(); uniq = []
    for r in rel:
        if r in seen: continue
        seen.add(r); uniq.append(r)
    out = []
    for path in uniq[:15]:
        full = "https://www.globenewswire.com" + path
        title_slug = path.rsplit("/", 1)[-1].replace(".html","")
        title = title_slug.replace("-", " ")
        out.append({"url": full, "title": title})
    return out

DATE_PATTERNS = [
    # "Wednesday, July 22, 2026" or "July 22, 2026"
    re.compile(r"\b(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})", re.I),
]
TIME_PATTERN = re.compile(r"(\d{1,2}:\d{2})\s*(a\.?m\.?|p\.?m\.?)\s*(?:\([^)]*\))?\s*(?:Eastern\s*Time|ET|EDT|EST)?", re.I)

def parse_announcement(url):
    """Fetch a wire press release page and extract earnings release/call dates."""
    text = fetch(url)
    if not text: return None
    txt = html.unescape(re.sub(r"<[^>]+>", " ", text))
    txt = re.sub(r"\s+", " ", txt)

    # Look for "release [its X quarter results] [on|after the market closes|...] DATE"
    # And "host an earnings conference call ... DATE ... at TIME"
    release_date = None
    call_date = None
    call_time = None

    # Find a release_date phrase
    m_rel = re.search(r"(?:release|report|announce).{0,80}(?:results|earnings).{0,80}(?:on|after\s+the\s+market\s+(?:close|opens)\s+(?:on|of)?)\s*([A-Z][a-z]+day,?\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4})", txt, re.I)
    if not m_rel:
        m_rel = re.search(r"(?:release|report|announce).{0,80}(?:results|earnings).{0,120}((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4})", txt, re.I)
    if m_rel:
        release_date = m_rel.group(1)

    # Find a call_date + call_time phrase
    m_call = re.search(r"(?:conference\s+call|webcast|earnings\s+call).{0,200}((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}).{0,100}(\d{1,2}:\d{2}\s*[ap]\.?m\.?\s*(?:Eastern\s*Time|ET|EDT|EST)?)", txt, re.I)
    if m_call:
        call_date = m_call.group(1)
        call_time = m_call.group(2)
    else:
        # Just look for a time near 'call' or 'webcast'
        m_call2 = re.search(r"(?:conference\s+call|webcast|earnings\s+call).{0,200}(\d{1,2}:\d{2}\s*[ap]\.?m\.?\s*(?:Eastern\s*Time|ET|EDT|EST)?)", txt, re.I)
        if m_call2:
            call_time = m_call2.group(1)

    if release_date or call_date or call_time:
        return {"release_date": release_date, "call_date": call_date, "call_time": call_time}
    return None

def normalize_date(s):
    """Best-effort: 'July 22, 2026' or 'Wednesday, July 22, 2026' → 'YYYY-MM-DD'."""
    if not s: return None
    for fmt in ("%A, %B %d, %Y", "%B %d, %Y", "%B %d %Y"):
        try:
            return datetime.strptime(s.replace(",", "").replace("  "," "), fmt.replace(",","")).date().isoformat()
        except: pass
    # extract month/day/year
    m = DATE_PATTERNS[0].search(s)
    if m:
        try:
            mn = datetime.strptime(m.group(1)[:3], "%b").month
            return f"{int(m.group(3))}-{mn:02d}-{int(m.group(2)):02d}"
        except: pass
    return s  # return as-is

def main():
    log("=== Save-the-date sweep starting ===")
    confirmed = {}
    if OUT_FILE.exists():
        confirmed = json.loads(OUT_FILE.read_text())

    found_count = 0
    for tk, (company, slug) in TIER_1_GLOBENEWSWIRE.items():
        log(f"Sweeping GlobeNewswire for {tk} ({company})")
        releases = gnw_titles(slug)
        time.sleep(0.4)
        for r in releases:
            title = r["title"].lower()
            if not SAVE_THE_DATE_TITLE.search(title): continue
            if not EARNINGS_KW.search(title): continue
            if "earnings release" not in title and "earnings call" not in title \
               and "conference call" not in title and "confirms" not in title \
               and "results announcement" not in title:
                continue
            log(f"  Candidate: {r['title'][:100]}")
            details = parse_announcement(r["url"])
            time.sleep(0.4)
            if not details: continue
            rec = {
                "release_date_raw": details.get("release_date"),
                "release_date":     normalize_date(details.get("release_date")),
                "call_date_raw":    details.get("call_date"),
                "call_date":        normalize_date(details.get("call_date")),
                "call_time":        details.get("call_time"),
                "source_url":       r["url"],
                "source_title":     r["title"][:200],
                "captured_at":      datetime.now().isoformat(timespec="seconds"),
            }
            # Filter out past dates
            today_iso = date.today().isoformat()
            future_date = rec["release_date"] or rec["call_date"]
            if future_date and future_date < today_iso:
                log(f"    (skipping past announcement: {future_date})")
                continue
            confirmed.setdefault(tk, [])
            # Dedup by source_url
            if not any(x.get("source_url") == rec["source_url"] for x in confirmed[tk]):
                confirmed[tk].append(rec)
                found_count += 1
                log(f"    + {tk}: release={rec['release_date']} call={rec['call_date']} {rec['call_time']}")

    OUT_FILE.write_text(json.dumps(confirmed, indent=1))
    log(f"=== Done. {found_count} new save-the-dates captured. ===\n")

if __name__ == "__main__":
    main()
