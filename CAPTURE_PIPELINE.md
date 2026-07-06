# Earnings-call capture pipeline

Automated capture → transcription → publication of public-company earnings
calls. Built June–July 2026; first fully automated cloud run 2026-07-06
(Actions run 28825876604: Comcast Q1 2026 replay, 13.3 minutes unattended).

## How it works

```
transcript_watchlist.json          upcoming_dates.json
  (who to capture, IR URL)           (when calls happen)
            \                        /
             call-capture.yml  (11:03 & 17:03 UTC weekdays)
                    |
        1. resolve_webcast.py --ticker X --mode live|replay
             Stage A: scrape IR events page (curl_cffi, Chrome impersonation)
                      → player URL, direct MP3s, official transcript PDFs
             Stage B: headless Playwright opens the player, fills the
                      registration form, sniffs the HLS audio manifest
                      from the browser's network responses
             → capture_state.json: resolved_stream_url
                    |
        2. capture_call.py capture     ffmpeg pulls the m3u8 → 16 kHz mono MP3
        3. capture_call.py transcribe  faster-whisper (small, int8, CPU)
        4. capture_call.py publish     password-gated HTML with disclaimer,
                                       source line, official replay link
                    |
             commit + push → GitHub Pages
```

The morning run (11:03 UTC = 7:03 ET) sleeps until 5 minutes before the call
and attempts **live** capture (2.5 h hard cap). The afternoon run (17:03 UTC)
skips if a transcript already exists, otherwise resolves the **replay**
(typically posted 1–3 h after the call). Failures set `pending_replay` in
`capture_state.json` and are retried on the next run — the workflow never
hard-fails on a missing stream.

## Adding a company

Add one entry to `earnings_data/transcript_watchlist.json`:

```json
"TICK": {
  "name": "Company Name",
  "live": true,
  "ir_events_url": "https://<IR site>/events-and-presentations",
  "next_call_date": "YYYY-MM-DD",
  "next_call_time": "HH:MM ET"
}
```

Finding the IR events page: check the company's earnings press release for
the IR site URL, or `dig +short <candidate domain>` — a CNAME to
`*.gcs-web.com` means Notified (verified working: Comcast), `*.q4web.com`
means Q4 (events pages verified scrapeable for JNJ/FMC/CRS; the Q4 player's
registration flow has not been sniff-tested yet). `webcast_url` is an
optional fallback player URL used if the events-page scrape fails.

Quarter matching is automatic and accounts for the reported-quarter offset
(a July call is the "Q2" event).

## Manual run

Actions → "Capture earnings call" → Run workflow with `ticker` and `date`
inputs (skips the sleep step). Locally: run the three `capture_call.py`
stages in order after `resolve_webcast.py`; note ffmpeg is not installed on
the local Mac — use `yt-dlp --downloader native <m3u8>` there instead.

## Design decisions

- **Network-sniffing, not API reverse-engineering.** Stage B watches what
  the browser fetches, so it works on any player a desktop browser can play
  without email verification, regardless of vendor.
- **Registration** uses a designated non-personal identity (form fields
  `firstname/lastname/email/institution` on Notified players).
- **YouTube is not a source.** PO-token enforcement plus datacenter-IP
  flagging make yt-dlp unreliable on CI runners.
- **Machine transcripts are drafts.** Whisper output matches the official
  LSEG transcript phrase-for-phrase in spot checks, but proper names garble.
  Every published page carries a verify-before-quoting disclaimer and links
  the official replay; when Stage A finds the company's official transcript
  PDF it is linked on the dashboard as "Company IR."
- **Legal basis:** earnings calls are public Reg FD events; Swatch Group v.
  Bloomberg (2d Cir. 2014) held recording them for news reporting is fair use.

## Costs

None. Public-repo Actions minutes, open-source tools (Playwright, curl_cffi,
ffmpeg, faster-whisper), no API keys.
