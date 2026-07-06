#!/usr/bin/env python3
"""Resolve the live or replay stream URL for an earnings call webcast.

Stage A (gcs-web parser, curl_cffi):
    Fetch the IR events page (Notified/gcs-web platform), find the event-details
    link matching the quarter, extract the edge.media-server.com player URL,
    any direct .mp3, and the official transcript PDF link.

Stage B (Playwright, only if no direct .mp3):
    Open the player page in headless chromium, auto-fill the registration form,
    sniff the HLS/audio stream URL from network responses.

Results are written into earnings_data/capture_state.json under the ticker
(merged, no other keys clobbered).

CLI:
    python3 resolve_webcast.py --ticker CMCSA --mode live|replay [--player-url URL]

--player-url overrides everything (useful for manual testing without a network hit).
"""
import argparse, json, re, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

HERE = Path(__file__).parent
ED = HERE / "earnings_data"
WATCHLIST_FILE = ED / "transcript_watchlist.json"
STATE_FILE = ED / "capture_state.json"
TRANSCRIPTS_FILE = ED / "transcripts.json"

# ---------------------------------------------------------------------------
# Logging — match capture_call.py exactly
# ---------------------------------------------------------------------------

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# JSON helpers — match capture_call.py exactly
# ---------------------------------------------------------------------------

def load_watchlist():
    if not WATCHLIST_FILE.exists():
        sys.exit("resolve_webcast.py: transcript_watchlist.json not found")
    return json.loads(WATCHLIST_FILE.read_text())


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=1))


def load_transcripts():
    if TRANSCRIPTS_FILE.exists():
        return json.loads(TRANSCRIPTS_FILE.read_text())
    return {}


def save_transcripts(data):
    TRANSCRIPTS_FILE.write_text(json.dumps(data, indent=1, sort_keys=True))


# ---------------------------------------------------------------------------
# Quarter helpers
# ---------------------------------------------------------------------------

def candidate_quarters(date_str):
    """Return [(Q, year), ...] candidates for a call on YYYY-MM-DD, most likely first.

    An earnings call reports the PRIOR quarter (a July call is the Q2 call),
    so the reported quarter comes first; the calendar quarter of the call date
    is kept as a fallback for companies with offset fiscal years.
    """
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return []
    cal_q = (dt.month - 1) // 3 + 1
    reported_q, reported_year = (cal_q - 1, dt.year) if cal_q > 1 else (4, dt.year - 1)
    return [(reported_q, reported_year), (cal_q, dt.year)]


def slug_matches_quarter(slug, q, year):
    """True if slug contains q{N}-{year} matching the given quarter."""
    pattern = rf"q{q}-{year}"
    return bool(re.search(pattern, slug, re.I))


def slug_contains_earnings(slug):
    return "earnings" in slug.lower()


# ---------------------------------------------------------------------------
# Stage A: gcs-web parser (curl_cffi)
# ---------------------------------------------------------------------------

_PLAYER_RE = re.compile(
    r'href=["\']?(https?://edge\.media-server\.com/mmc/p/[^"\'>\s]+)',
    re.I
)
_MP3_RE = re.compile(r'href=["\']?([^"\'>\s]+\.mp3)["\']?', re.I)
_EVENT_HREF_RE = re.compile(r'/events/event-details/([^"\'>\s/]+)', re.I)
# PDF match: any /static-files/ link with title containing "Transcript"
_PDF_TITLE_RE = re.compile(
    r'<a\s[^>]*href=["\']?(/static-files/[^"\'>\s]+)["\']?[^>]*title=["\']([^"\']*)["\']',
    re.I
)


def fetch_html_cffi(url, timeout=30, retries=1):
    """Fetch URL using curl_cffi Chrome impersonation. Returns (html, status_code)."""
    try:
        from curl_cffi import requests as cffi_req
    except ImportError:
        log("ERROR: curl_cffi not installed. Run: pip install curl_cffi")
        return "", 0

    for attempt in range(retries + 1):
        try:
            r = cffi_req.get(url, impersonate="chrome", timeout=timeout)
            if r.status_code == 200:
                return r.text, 200
            log(f"  fetch {url}: HTTP {r.status_code} (attempt {attempt + 1})")
            if attempt < retries:
                import time; time.sleep(3)
        except Exception as e:
            log(f"  fetch error {url} (attempt {attempt + 1}): {e}")
            if attempt < retries:
                import time; time.sleep(3)
    return "", 0


def extract_event_links(html, base_url):
    """Return list of (slug, absolute_url) for /events/event-details/ hrefs."""
    results = []
    seen = set()
    for m in _EVENT_HREF_RE.finditer(html):
        slug = m.group(1)
        if slug in seen:
            continue
        seen.add(slug)
        abs_url = urljoin(base_url, f"/events/event-details/{slug}")
        results.append((slug, abs_url))
    return results


def extract_from_event_page(html, base_url):
    """Extract player_url, mp3_url, transcript_pdf_url from an event-details page.

    Returns dict with any of: player_url, mp3_url, transcript_pdf_url.
    """
    result = {}

    # Player URL (edge.media-server.com)
    m = _PLAYER_RE.search(html)
    if m:
        result["player_url"] = m.group(1).rstrip("'\"")
        log(f"  Found player URL: {result['player_url']}")

    # Direct .mp3
    m = _MP3_RE.search(html)
    if m:
        url = m.group(1)
        if not url.startswith("http"):
            url = urljoin(base_url, url)
        result["mp3_url"] = url
        log(f"  Found direct MP3: {result['mp3_url']}")

    # Transcript PDF — first try broad title-attribute scan
    for tm in _PDF_TITLE_RE.finditer(html):
        href, title = tm.group(1), tm.group(2)
        if "transcript" in title.lower():
            result["transcript_pdf_url"] = urljoin(base_url, href)
            log(f"  Found transcript PDF (title attr): {result['transcript_pdf_url']}")
            break

    # Fallback: link text containing "Transcript"
    if "transcript_pdf_url" not in result:
        pdf_link_re = re.compile(
            r'<a\s[^>]*href=["\']?(/static-files/[^"\'>\s]+)["\']?[^>]*>([^<]*Transcript[^<]*)<',
            re.I
        )
        m = pdf_link_re.search(html)
        if m:
            result["transcript_pdf_url"] = urljoin(base_url, m.group(1))
            log(f"  Found transcript PDF (link text): {result['transcript_pdf_url']}")

    return result


def stage_a(entry, next_call_date, player_url_override=None):
    """Run Stage A: gcs-web parser.

    Returns dict with any of: player_url, mp3_url, transcript_pdf_url.
    player_url_override bypasses all fetching.
    """
    if player_url_override:
        log(f"Stage A: --player-url override, skipping IR fetch")
        return {"player_url": player_url_override}

    ir_events_url = entry.get("ir_events_url")
    fallback_player = entry.get("webcast_url")

    if not ir_events_url:
        log("Stage A: no ir_events_url in watchlist entry")
        if fallback_player:
            log(f"Stage A: using watchlist webcast_url as player: {fallback_player}")
            return {"player_url": fallback_player}
        return {}

    parsed_base = urlparse(ir_events_url)
    base_url = f"{parsed_base.scheme}://{parsed_base.netloc}"

    log(f"Stage A: fetching IR events page {ir_events_url}")
    html, status = fetch_html_cffi(ir_events_url, timeout=30, retries=1)
    if not html:
        log(f"Stage A: IR events page fetch failed (status={status})")
        if fallback_player:
            log(f"Stage A: falling back to watchlist webcast_url: {fallback_player}")
            return {"player_url": fallback_player}
        return {}

    event_links = extract_event_links(html, base_url)
    log(f"Stage A: found {len(event_links)} event-details links")

    if not event_links:
        if fallback_player:
            log(f"Stage A: no event links found; falling back to webcast_url: {fallback_player}")
            return {"player_url": fallback_player}
        return {}

    # Pick the best matching event link. Try the reported quarter first
    # (a July call is the Q2 call), then the calendar quarter of the call date.
    target_slug = None
    target_url = None

    for q, year in candidate_quarters(next_call_date) if next_call_date else []:
        for slug, url in event_links:
            if slug_matches_quarter(slug, q, year) and slug_contains_earnings(slug):
                target_slug, target_url = slug, url
                log(f"Stage A: matched Q{q} {year} earnings event: {slug}")
                break
        if target_url:
            break

    if not target_url:
        # Fallback: prefer an earnings slug mentioning the call year, else any earnings slug
        call_year = (next_call_date or "")[:4]
        earnings_links = [(s, u) for s, u in event_links if slug_contains_earnings(s)]
        for slug, url in earnings_links:
            if call_year and call_year in slug:
                target_slug, target_url = slug, url
                log(f"Stage A: fallback to earnings slug with year {call_year}: {slug}")
                break
        if not target_url and earnings_links:
            target_slug, target_url = earnings_links[0]
            log(f"Stage A: fallback to first earnings slug: {target_slug}")

    if not target_url:
        target_slug, target_url = event_links[0]
        log(f"Stage A: fallback to first event link: {target_slug}")

    log(f"Stage A: fetching event page {target_url}")
    event_html, ev_status = fetch_html_cffi(target_url, timeout=30, retries=1)
    if not event_html:
        log(f"Stage A: event page fetch failed (status={ev_status})")
        if fallback_player:
            return {"player_url": fallback_player}
        return {}

    result = extract_from_event_page(event_html, base_url)

    # If no player found on event page, try fallback
    if not result.get("player_url") and not result.get("mp3_url"):
        if fallback_player:
            log(f"Stage A: no player/mp3 found on event page; using webcast_url: {fallback_player}")
            result["player_url"] = fallback_player

    return result


# ---------------------------------------------------------------------------
# Stage B: Playwright browser (lazy import)
# ---------------------------------------------------------------------------

# Sniff rule from spec: prefer audio+.m3u8, else any .m3u8, else .mp3; ignore .ts
_MPEGURL_CT = re.compile(r"mpegurl|audio/", re.I)
_AUDIO_URL_RE = re.compile(r"\.(m3u8|mp3|aac)($|\?)", re.I)
_TS_URL_RE = re.compile(r"\.ts($|\?)", re.I)


def _score_url(url):
    """Higher = more preferred. Returns -1 if should ignore."""
    if _TS_URL_RE.search(url):
        return -1
    if "audio" in url and ".m3u8" in url:
        return 3
    if ".m3u8" in url:
        return 2
    if ".mp3" in url:
        return 1
    if ".aac" in url:
        return 0
    return -1


def stage_b(player_url, mode):
    """Run Stage B: Playwright browser to sniff stream URL.

    Returns stream URL string or None.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log("ERROR: playwright not installed. Run: pip install playwright && python3 -m playwright install chromium")
        return None

    log(f"Stage B: launching headless chromium for {player_url}")
    best_url = None
    best_score = -1

    def on_response(response):
        nonlocal best_url, best_score
        try:
            ct = response.headers.get("content-type", "")
            url = response.url
            if _TS_URL_RE.search(url):
                return
            is_media_ct = bool(_MPEGURL_CT.search(ct))
            is_media_url = bool(_AUDIO_URL_RE.search(url))
            if not (is_media_ct or is_media_url):
                return
            score = _score_url(url)
            if score > best_score:
                best_score = score
                best_url = url
                log(f"  Stage B: sniffed candidate (score={score}): {url[:120]}")
        except Exception:
            pass

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.on("response", on_response)

            log(f"Stage B: navigating to {player_url}")
            page.goto(player_url, wait_until="domcontentloaded", timeout=60000)

            # Cookie banner (may be absent)
            try:
                page.click("#onetrust-accept-btn-handler", timeout=5000)
                log("Stage B: accepted cookie banner")
            except PWTimeout:
                pass

            # Registration form — fill all four fields
            log("Stage B: filling registration form")
            try:
                page.fill('input[name="firstname"]', "a", timeout=15000)
                page.fill('input[name="lastname"]', "g", timeout=5000)
                page.fill('input[name="email"]', "avsgithubemail@gmail.com", timeout=5000)
                page.fill('input[name="institution"]', "personal", timeout=5000)
            except PWTimeout as e:
                log(f"Stage B: form fill timeout: {e}")
                browser.close()
                return None

            # Submit button (may be disabled until fields filled)
            log("Stage B: submitting form")
            submitted = False
            for selector in [
                'button:has-text("Submit")',
                'button[type="submit"]',
                'input[type="submit"]',
            ]:
                try:
                    page.click(selector, timeout=8000)
                    submitted = True
                    log(f"  Stage B: clicked submit via {selector!r}")
                    break
                except PWTimeout:
                    continue

            if not submitted:
                log("Stage B: could not click submit button")
                browser.close()
                return None

            # Wait up to 45s for a media response, checking every 5s
            # (the player auto-plays after submit; a manifest usually lands within 10s)
            log("Stage B: waiting up to 45s for stream URL...")
            for _ in range(9):
                page.wait_for_timeout(5000)
                if best_score >= 2:  # got an .m3u8 — no need to keep waiting
                    break

            if not best_url:
                # No stream yet — try a play button before giving up
                for play_sel in [
                    'button[aria-label*="lay" i]',
                    'button[class*="play" i]',
                    '.vjs-big-play-button',
                ]:
                    try:
                        page.click(play_sel, timeout=3000)
                        log(f"  Stage B: play-clicked (pre-stream) via {play_sel!r}")
                        page.wait_for_timeout(10000)
                        break
                    except PWTimeout:
                        continue

            browser.close()

    except Exception as e:
        log(f"Stage B: unexpected error: {e}")
        return None

    if best_url:
        log(f"Stage B: resolved stream URL: {best_url}")
    else:
        log("Stage B: no stream URL captured")
    return best_url


# ---------------------------------------------------------------------------
# transcripts.json helper (bonus: official transcript PDF link)
# ---------------------------------------------------------------------------

def upsert_transcript_pdf(ticker, pdf_url, call_date):
    """Add a Company IR transcript entry to transcripts.json if not already present."""
    transcripts = load_transcripts()
    existing_urls = {r.get("url") for r in transcripts.get(ticker, [])}
    if pdf_url in existing_urls:
        log(f"transcripts.json: {ticker} already has this PDF URL — skipping")
        return
    record = {
        "url": pdf_url,
        "source": "Company IR",
        "title": f"{ticker} Earnings Call Transcript (Official PDF)",
        "date_str": call_date or datetime.now(timezone.utc).date().isoformat(),
        "found_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    transcripts.setdefault(ticker, []).insert(0, record)
    save_transcripts(transcripts)
    log(f"transcripts.json: added Company IR PDF for {ticker}")


# ---------------------------------------------------------------------------
# Main resolve flow
# ---------------------------------------------------------------------------

def do_resolve(ticker, mode, player_url_override=None):
    watchlist = load_watchlist()
    state = load_state()

    entry = watchlist.get(ticker)
    if not entry:
        log(f"ERROR: {ticker} not in transcript_watchlist.json")
        state.setdefault(ticker, {})["pending_replay"] = True
        save_state(state)
        sys.exit(0)

    next_call_date = entry.get("next_call_date")

    # Stage A
    log(f"=== resolve_webcast: {ticker} mode={mode} ===")
    a_result = stage_a(entry, next_call_date, player_url_override)

    player_url = a_result.get("player_url")
    mp3_url = a_result.get("mp3_url")
    transcript_pdf_url = a_result.get("transcript_pdf_url")

    stream_url = mp3_url  # direct mp3 → skip Stage B

    # Stage B (only if no direct mp3)
    if not stream_url and player_url:
        log("No direct MP3 found — proceeding to Stage B (Playwright)")
        stream_url = stage_b(player_url, mode)
    elif not stream_url and not player_url:
        log("ERROR: Stage A produced neither a player URL nor a direct MP3")
        state.setdefault(ticker, {})["pending_replay"] = True
        save_state(state)
        sys.exit(0)

    # Write results into capture_state.json
    now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ticker_state = state.get(ticker, {})
    if stream_url:
        ticker_state["resolved_stream_url"] = stream_url
        ticker_state.pop("pending_replay", None)
        log(f"Stream URL resolved: {stream_url}")
    else:
        log(f"ERROR: Could not resolve stream URL for {ticker}. Setting pending_replay.")
        ticker_state["pending_replay"] = True
        ticker_state["pending_replay_date"] = next_call_date or datetime.now(timezone.utc).date().isoformat()

    ticker_state["resolved_player_url"] = player_url or ""
    ticker_state["resolved_at"] = now_utc
    ticker_state["resolve_mode"] = mode
    if transcript_pdf_url:
        ticker_state["transcript_pdf_url"] = transcript_pdf_url

    state[ticker] = ticker_state
    save_state(state)
    log(f"capture_state.json updated for {ticker}")

    # Bonus: upsert into transcripts.json if PDF found
    if transcript_pdf_url:
        upsert_transcript_pdf(ticker, transcript_pdf_url, next_call_date)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Resolve earnings call stream URL (Stage A: gcs-web; Stage B: Playwright)"
    )
    parser.add_argument("--ticker", required=True, help="Ticker symbol, e.g. CMCSA")
    parser.add_argument(
        "--mode",
        required=True,
        choices=["live", "replay"],
        help="'live' for morning run, 'replay' for afternoon/retry run",
    )
    parser.add_argument(
        "--player-url",
        metavar="URL",
        default=None,
        help="Override player URL (skips Stage A IR fetch; for testing)",
    )
    args = parser.parse_args()
    do_resolve(args.ticker, args.mode, args.player_url)


if __name__ == "__main__":
    main()
