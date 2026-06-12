# Earnings tracker changelog

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
