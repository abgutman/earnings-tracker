#!/usr/bin/env python3
"""Local-company news feed — replaces the old Yahoo ticker firehose.

Reads earnings_data/local_roster.json (the expert-editable roster of
Philadelphia-area companies we actually cover) and pulls each entity's news /
press releases from its configured source(s):

  yahoo_search  public company, by ticker (Yahoo Finance search) — all news
  wire_page     PR Newswire per-company page (scrape release title/date/link)
  rss           native RSS/Atom feed (feedparser)
  html_scrape   server-rendered newsroom listing (per-site; stub for now)

Items are normalized to the existing news_feed.json schema, deduped (within an
entity by title+date so a public-company release seen via both Yahoo and the
wire collapses to one; globally by uuid), pruned to a lookback window, and
written to earnings_data/news_feed.json — the same file the Busy Biz homepage
panel already reads.

EDGAR is intentionally NOT pulled here (covered by the earnings 8-K pipeline);
Yahoo's EDGAR-derived headlines are kept, not filtered. Only roster entities
with a confirmed working source are active:true, so the feed never implies
coverage it doesn't have.
"""
import calendar
import hashlib
import html as _html
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).parent
ED = HERE / "earnings_data"
ROSTER_FILE = ED / "local_roster.json"
FEED_FILE = ED / "news_feed.json"
LOG_FILE = ED / "news_feed_log.txt"

LOOKBACK_HOURS = 720  # 30 days — press releases are lower-volume than the old firehose
ET = ZoneInfo("America/New_York")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Analyst/aggregator publishers we never want in the feed (Yahoo surfaces these).
EXCLUDED_PUBLISHERS = {
    "StockStory", "Motley Fool", "GuruFocus.com", "Simply Wall St.",
    "24/7 Wall St.", "Seeking Alpha", "InvestorPlace", "TipRanks",
    "Insider Monkey", "Barchart", "Finbold", "MarketBeat", "Benzinga",
    "Investing.com", "Stockopedia", "Schaeffer's", "Zacks",
}

STRIP_TAGS = re.compile(r"<[^>]+>")

# PR Newswire per-company card: link + <small>date</small> title </h3>
PRN_CARD = re.compile(
    r'newsreleaseconsolidatelink[^"]*"\s+href="(?P<href>/news-releases/[^"]+\.html)".*?'
    r'<small[^>]*>(?P<date>[A-Z][a-z]{2} \d{1,2}, \d{4}[^<]*)</small>(?P<title>.*?)</h3>',
    re.S)


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, file=sys.stderr)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# --- HTTP helpers ------------------------------------------------------------
def _curl(url, timeout=20):
    """Return response text: curl_cffi (chrome TLS) first, urllib fallback."""
    try:
        from curl_cffi import requests as creq
        r = creq.get(url, impersonate="chrome120", timeout=timeout)
        return r.text if r.status_code == 200 else None
    except ImportError:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception:
            return None
    except Exception as e:
        log(f"  fetch err {url}: {e}")
        return None


def fetch_json(url, timeout=15):
    txt = _curl(url, timeout)
    if not txt:
        return None
    try:
        return json.loads(txt)
    except Exception:
        return None


# --- normalization helpers ---------------------------------------------------
def _uuid_for(link, title):
    seed = link or title or ""
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()


def _norm_title(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def _clean(text):
    return _html.unescape(STRIP_TAGS.sub("", text or "")).strip()


def _relevant(title, ticker, names):
    """Keep a Yahoo item only if it's actually about this company — not a peer
    Yahoo tagged onto the ticker. Match: standalone ticker, a full multi-word
    name/alias phrase, or a distinctive single-word name (>=5 chars)."""
    if ticker and re.search(r"(?<![A-Za-z])" + re.escape(ticker.upper()) + r"(?![A-Za-z])",
                            title.upper()):
        return True
    nt = _norm_title(title)
    for nm in names:
        nn = _norm_title(nm)
        if not nn:
            continue
        if " " in nn:                       # multi-word -> require the whole phrase
            if nn in nt:
                return True
        elif len(nn) >= 5:                  # single distinctive token
            if re.search(r"\b" + re.escape(nn) + r"\b", nt):
                return True
    return False


def _parse_prn_date(s):
    s = re.sub(r"\s*ET\s*$", "", (s or "").strip())
    for fmt in ("%b %d, %Y, %H:%M", "%b %d, %Y"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=ET)
            return int(dt.timestamp())
        except ValueError:
            continue
    return 0


# --- adapters ----------------------------------------------------------------
def adapter_yahoo_search(entity, source):
    """Public company: Yahoo search by ticker. Guarded against the generic
    fallback bucket Yahoo returns when a query doesn't resolve to a security."""
    ticker = (source.get("ticker") or "").strip()
    if not ticker:
        return []
    data = fetch_json(
        f"https://query1.finance.yahoo.com/v1/finance/search?q={ticker}&newsCount=25&quotesCount=3")
    if not data:
        return []
    syms = {(q.get("symbol") or "").upper() for q in data.get("quotes", [])}
    if ticker.upper() not in syms:
        log(f"  {entity['name']}: ticker {ticker} did not resolve — skipping (avoids generic bucket)")
        return []
    names = [entity["name"]] + entity.get("aliases", [])
    out = []
    for item in data.get("news", []):
        publisher = item.get("publisher", "")
        if publisher in EXCLUDED_PUBLISHERS:
            continue
        title = item.get("title", "")
        if not title:
            continue
        if not _relevant(title, ticker, names):
            continue
        link = item.get("link", "")
        out.append({
            "uuid": item.get("uuid") or _uuid_for(link, title),
            "tickers": [ticker],
            "company": entity["name"],
            "title": title,
            "publisher": publisher,
            "link": link,
            "published_unix": item.get("providerPublishTime", 0),
            "source_type": "yahoo",
        })
    return out


def adapter_wire_page(entity, source):
    """Private/nonprofit company: scrape the PR Newswire per-company page."""
    url = source.get("url")
    if not url:
        return []
    html = _curl(url)
    if not html:
        return []
    base = re.match(r"(https?://[^/]+)", url).group(1)
    out = []
    for m in PRN_CARD.finditer(html):
        title = _clean(m.group("title"))
        if not title:
            continue
        link = base + m.group("href")
        out.append({
            "uuid": _uuid_for(link, title),
            "tickers": [],
            "company": entity["name"],
            "title": title,
            "publisher": "PR Newswire",
            "link": link,
            "published_unix": _parse_prn_date(m.group("date")),
            "source_type": "wire",
        })
    return out


def adapter_rss(entity, source):
    """Native RSS/Atom feed."""
    import feedparser
    url = source.get("url")
    if not url:
        return []
    txt = _curl(url)
    if not txt:
        return []
    fp = feedparser.parse(txt)
    out = []
    for e in fp.entries[:25]:
        title = _clean(e.get("title", ""))
        if not title:
            continue
        link = e.get("link", "")
        pp = e.get("published_parsed") or e.get("updated_parsed")
        pub_unix = calendar.timegm(pp) if pp else 0  # feedparser struct_time is UTC
        out.append({
            "uuid": _uuid_for(link, title),
            "tickers": [],
            "company": entity["name"],
            "title": title,
            "publisher": entity["name"],
            "link": link,
            "published_unix": pub_unix,
            "source_type": "rss",
        })
    return out


def adapter_html_scrape(entity, source):
    """Server-rendered newsroom listing. Per-site selectors added incrementally."""
    log(f"  html_scrape not yet implemented for {entity['name']} ({source.get('url')})")
    return []


ADAPTERS = {
    "yahoo_search": adapter_yahoo_search,
    "wire_page": adapter_wire_page,
    "rss": adapter_rss,
    "html_scrape": adapter_html_scrape,
}


# --- feed I/O ----------------------------------------------------------------
def load_feed():
    if FEED_FILE.exists():
        return json.loads(FEED_FILE.read_text())
    return {"items": []}


def save_feed(feed):
    FEED_FILE.write_text(json.dumps(feed, indent=1))


def main():
    log("=== Local news feed poll start ===")
    roster = json.loads(ROSTER_FILE.read_text())
    entities = [e for e in roster.get("entities", []) if e.get("active")]
    log(f"{len(entities)} active entities")

    feed = load_feed()
    by_uuid = {it["uuid"]: it for it in feed.get("items", []) if it.get("uuid")}
    # (company, normalized-title) -> uuid, for cross-source dedupe within an entity
    seen_keys = {}
    for it in by_uuid.values():
        seen_keys[(it.get("company", "").lower(), _norm_title(it.get("title")))] = it["uuid"]

    new_count = 0
    for ent in entities:
        for src in ent.get("sources", []):
            adapter = ADAPTERS.get(src.get("type"))
            if not adapter:
                continue
            try:
                items = adapter(ent, src)
            except Exception as e:
                log(f"  {ent['name']} [{src.get('type')}] error: {e}")
                items = []
            time.sleep(0.2)
            added = 0
            for it in items:
                it["captured_at"] = datetime.now().isoformat(timespec="seconds")
                key = (ent["name"].lower(), _norm_title(it["title"]))
                if key in seen_keys:  # same release from another source — merge tickers
                    ex = by_uuid.get(seen_keys[key])
                    if ex:
                        for t in it.get("tickers", []):
                            if t and t not in ex.setdefault("tickers", []):
                                ex["tickers"].append(t)
                    continue
                if it["uuid"] in by_uuid:
                    continue
                by_uuid[it["uuid"]] = it
                seen_keys[key] = it["uuid"]
                added += 1
            new_count += added
            log(f"  {ent['name']} [{src.get('type')}]: {len(items)} fetched, {added} new")

    cutoff = datetime.now(timezone.utc).timestamp() - LOOKBACK_HOURS * 3600
    kept = [v for v in by_uuid.values() if v.get("published_unix", 0) >= cutoff]
    kept.sort(key=lambda x: x.get("published_unix", 0), reverse=True)

    save_feed({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "items": kept,
    })
    log(f"=== Done. {new_count} new items, {len(kept)} total in {LOOKBACK_HOURS}h window ===")


if __name__ == "__main__":
    main()
