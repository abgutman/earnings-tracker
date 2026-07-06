# Earnings tracker changelog

## 2026-07-06 — Webcast resolve stage: earnings-call capture is now fully automated

### What changed

**New:** `resolve_webcast.py` — the missing link between "we know a call is happening" and "we have audio to transcribe."

- **Stage A** fetches the company's IR events page (`ir_events_url` in the watchlist) with curl_cffi Chrome impersonation (plain requests are Akamai-blocked), finds the event matching the reported quarter (a July call is the Q2 call — the matcher tries reported quarter first, calendar quarter as fiscal-year fallback), and extracts the webcast player URL, any direct MP3, and official transcript PDFs.
- **Stage B** opens the player in headless Playwright chromium, auto-fills the soft registration form, and sniffs the HLS audio manifest URL from the browser's network responses. Platform-agnostic: it never parses the player's API, it just watches what the browser fetches.
- Results land in `capture_state.json` (`resolved_stream_url` etc.); official transcript PDFs are also linked into `transcripts.json` as source "Company IR" so the dashboard shows them next to Motley Fool links.

**Changed:** `capture_call.py` prefers a fresh (<12h) resolved stream URL; ffmpeg is primary for HLS/media-server streams (reconnect flags, 2.5h live hard cap); YouTube is dropped as a source entirely (PO-token enforcement + datacenter-IP flagging make yt-dlp unreliable on CI). `call-capture.yml` installs chromium, runs resolve before capture (mode=live on the 11 UTC run, replay otherwise), and skips the afternoon pass when the morning live capture already produced a transcript.

**Corrected:** Comcast's IR site is `www.cmcsa.com` (Notified/gcs-web, server-rendered) — the June notes saying "investors.comcast.com is NXDOMAIN / Q4Inc JS-only SPA" pointed at the wrong domain. The Q2 2026 player URL was already published on the events page weeks before the call.

### Cloud rehearsal (run 28825876604, 2026-07-06) — full pipeline green on GitHub Actions

Dispatched against the real Q1 2026 replay: **resolve 10s → capture 50-min replay 38s → Whisper transcribe 682s → publish + commit. 13.3 minutes total, unattended.** Notified/Akamai served the GitHub datacenter IP without blocking — no local fallback needed. Spot-check of the Whisper output against Comcast's official LSEG StreetEvents transcript PDF: distinctive phrases match verbatim (proper names garble occasionally — that's what the verify-quotes disclaimer is for). Artifacts: `transcript_CMCSA_2026-04-23.html` (password-gated), `earnings_data/transcripts_raw/CMCSA_2026-04-23.txt`.

### July 23 (Q2 call, 8:30 a.m. ET) is now fully automatic

Morning run (11:03 UTC) sleeps to 8:25, resolves the live stream, captures, transcribes, publishes. Afternoon run (17:03 UTC) catches the replay (posted ~11:30 a.m. ET) if the live pass failed. No manual steps.

### Expanding to more companies

Add a watchlist entry with `ir_events_url` + `next_call_date`/`next_call_time`. Two platform families verified scrapeable so far: Notified/gcs-web (Comcast) and Q4 (`*.q4web.com` — JNJ, FMC, CRS; events page server-rendered, player registration flow not yet sniff-tested).

## 2026-07-06 — EDGAR firehose detection + cron de-clash

### EDGAR firehose (primary detection, ~2-minute lag)

**New:** `edgar-firehose` mode in `simple_earnings.py` + `edgar-firehose.yml` workflow.

**Why:** The submissions API (`data.sec.gov/submissions/`) indexes filings hours after SEC acceptance (observed June 3: accepted 12:25 PM ET, indexed ~4:30 PM). EDGAR's "latest filings" Atom feed (`browse-edgar?action=getcurrent`) shows new filings ~2 minutes after acceptance and one request covers the whole market.

**How it works:**
- Fetches the 8-K and 10-Q Atom feeds (100 entries each; 2–3 requests/run vs. the old ~100), filters entries client-side against the ~100 watched CIKs from `cache.json`.
- Classifies exactly like `find_latest_earnings_filing`: 8-K Item 2.02, 8-K Items 7.01+9.01, bare 10-Q. Exact form matches only — amended 8-K/A and 10-Q/A are ignored, same as the submissions path.
- Dedups on `last_detected_accession` (shared with edgar-poll, so the two modes can never double-email).
- On a hit, tries one submissions-API confirm for the primary-doc URL; if not yet indexed (common, given the lag), links the filing index page from the feed instead.
- Watermark in `earnings_data/firehose_state.json` (10-min overlap; fetches page 2 via `&start=100` when page 1 is entirely new — handles the 4:00–4:15 PM avalanche).
- Shared helper `process_new_filing()` extracted from `edgar_poll()` so cache updates and typed emails are identical in both modes.

**Schedule:** every 10 min pre/post-market (11–13, 20–21 UTC), every 30 min midday, weekdays. `edgar-poll-recent.yml` demoted to a 3x/day reconciliation sweep (`23 13,18,21 * * 1-5`) as the safety net.

### Cron de-clash + push hardening (all workflows)

- Every workflow moved off GitHub's high-load minutes (:00/:15/:30/:45), each on its own minute: news-feed :11, edgar-poll :23, transcript-harvest :43, yahoo-upcoming :53, call-capture :03, firehose :07/:17/.../:57. (Also: busy-biz quotes → :09/:39, bankruptcy-tracker → :21.) GitHub documents that top-of-hour schedules get delayed or skipped under load — the June 13–15 news-feed stall was likely this.
- Every workflow got `concurrency: group: ${{ github.workflow }}` (no overlapping runs of the same workflow) and a 3-attempt `git pull --rebase && git push` retry loop with backoff (absorbs cross-workflow push races).

## 2026-06-12 — Transcript link harvester + Whisper capture pipeline

### Phase 1 — Transcript link harvester

**New scripts:** `poll_fool_transcripts.py`, updated `simple_dashboard.py`.

**What it does:**
- Fetches the Motley Fool transcript index page (~20 most recent links) on each run and matches tickers by the `-<ticker>-q` slug pattern.
- Secondary path: Yahoo Finance news API for tickers Fool doesn't cover; filters by `providerPublishTime` to avoid stale results from prior quarters.
- Stores links only — no transcript text, per copyright policy (repo is public).
- `transcript_seeds.json` backfills CPB (June 8) and FIVE (June 4) known URLs for this cycle.
- `simple_dashboard.py` now reads `transcripts.json` and adds attributed "Transcript (Motley Fool) ↗" links on recent earnings rows.

**Automation:** `transcript-harvest.yml` runs 3x/weekday (15:00, 19:00, 23:00 UTC = 11 AM, 3 PM, 7 PM ET). First run committed on 2026-06-12 18:26 UTC — green.

**Known findings this cycle:** CPB and FIVE transcripts confirmed on Fool. URBN, COR, TOL: not on Fool for this quarter (all 404). Yahoo secondary finds no transcripts for those three.

### Phase 2 — Whisper capture pipeline

**New scripts:** `capture_call.py`, `earnings_data/transcript_watchlist.json`, `earnings_data/capture_state.json`.

**What it does:**
- `capture_call.py capture` — yt-dlp / ffmpeg audio download from watchlist URL.
- `capture_call.py transcribe` — faster-whisper (small, int8, CPU) with timestamped output.
- `capture_call.py publish` — builds password-gated HTML with machine-transcript disclaimer, "verify quotes before publishing" warning, source line, and replay link.
- `capture_call.py check-windows` — unit-style scheduler window test (4/4 passing locally).

**Automation:** `call-capture.yml` runs 2x/weekday (11:00 and 17:00 UTC = 7 AM and 1 PM ET), 6-hour windows. Supports `workflow_dispatch` with `ticker` and `date` inputs. Morning run retries `pending_replay` flags.

**Comcast (CMCSA) pilot:** `live=false`. `investors.comcast.com` is NXDOMAIN. Q4Inc webcast platform is JS-only SPA with auth-gated API. Live stream capture not feasible from a cloud runner. `replay_url` set to YouTube Q1 2026 recording for rehearsal setup.

**Rehearsal outcome:** BLOCKED by YouTube bot-detection on cloud runners (yt-dlp error: "Sign in to confirm you're not a bot"). The transcribe and publish steps were verified locally with macOS TTS-generated audio: faster-whisper correctly transcribed the audio; auth gate, disclaimer, and source line all confirmed present in published HTML. Replay audio source for the full on-Actions rehearsal remains blocked pending a non-YouTube URL (expected when Comcast posts the Q2 2026 webcast URL ~late June/early July).

### Scheduler window verification (gate 5)
Confirmed locally:
- 08:30 ET call covered by 11:00 UTC run (7 AM ET, 6h window) — PASS
- 16:00 ET call covered by 17:00 UTC run (1 PM ET, 6h window) — PASS
- 08:30 ET call NOT covered by 17:00 UTC run — PASS
- 16:00 ET call NOT covered by 11:00 UTC run — PASS

---

## 2026-06-12 — Comcast byline-date bug fix

### Incident summary
On June 11, 2026, `poll_upcoming.py` captured the Comcast Q2 2026 earnings announcement
(`CMCSA`) with wrong data: `call_date=2026-06-11`, `call_time=05:30 ET`. The actual
announcement stated: **conference call Thursday, July 23, 2026 at 8:30 a.m. ET**.

Root cause: `extract_from_body()` ran its "conference call ... DATE ... TIME" regex over
the entire page text including Yahoo's article header/byline ("...Conference Call Business
Wire Thu, June 11, 2026 at 5:30 AM PDT"). The byline match came first. Additionally,
`normalize_time()` relabelled all timezone tokens as ET without converting the hour,
so "5:30 AM PDT" became "05:30 ET" instead of "08:30 ET".

An email alert with the wrong date was sent to all three recipients on June 11 at 7:17 PM
ET. The entry appeared as "TODAY" on the upcoming dashboard that evening, then disappeared
on June 12 when the dashboard rebuilt (past entries skipped).

### Bugs fixed (poll_upcoming.py)
1. `extract_from_body()`: now anchors to wire dateline (`--( BUSINESS WIRE )--` etc.)
   before running regex, excluding the Yahoo header. Uses `re.finditer` + prefers the
   first match whose date is after the publish date.
2. `normalize_time()`: converts PDT/PST/MDT/MST/CDT/CST to ET with correct hour offset.
   Unknown zones keep their original label rather than being falsely stamped as ET.
3. Sanity guard in `main()`: if all extracted dates equal the article's publish date,
   logs "suspicious extraction, skipping" and does not store or send email.
4. Cleanup: treats `null` dates as absent rather than defaulting to "9999", which
   prevented null-date entries from being cleaned up.
5. `send_email()` return value now checked; "alert sent" logged only on `True`.

### Data fix
`earnings_data/upcoming_dates.json` (CMCSA entry):
- `call_date`: `2026-06-11` → `2026-07-23`
- `call_time`: `05:30 ET` → `08:30 ET`
- `source_type`: `yahoo_auto` → `manual_fix`

JNJ and BDN entries confirmed unchanged.

### Deploy changes
- `abgutman/earnings-tracker` re-cloned to `.deploy/earnings-tracker/` (was at
  `/tmp/earnings-tracker/`, which macOS had purged; standing rule: no deploy clones in
  `/tmp/`).
- `business/CLAUDE.md` updated to reflect the new clone path.
- `business/earnings/test_extract_upcoming.py` added as a regression test.
- `business/earnings/send_cmcsa_correction.py` added as a one-off correction email sender
  (requires `GMAIL_USER` and `GMAIL_APP_PASSWORD` in environment).

### Corrected email — SENT 2026-06-12
Gmail credentials are only in GitHub secrets, so the correction was sent via a one-off
manual-dispatch workflow on the `earnings-tracker` repo (run 27432100954, conclusion:
success). Subject "✉️ CORRECTION: Save the date: Comcast", sent to all three recipients
(`EMAIL_TO`), noting the June 11 error in the blurb. The one-off workflow and repo copy
of the script were removed after the confirmed send (commits `62b8aed` add, `4645a17`
remove). The local script is kept at `business/earnings/send_cmcsa_correction.py` as a
template for future correction emails.
