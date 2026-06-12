# Earnings tracker changelog

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
