#!/usr/bin/env python3
"""Earnings call audio capture, Whisper transcription, and HTML publication.

Three subcommands:

  capture  --ticker X [--date YYYY-MM-DD]
      Try yt-dlp against the configured webcast or replay URL.
      Falls back to ffmpeg for direct HLS/MP3 streams.
      Saves audio to earnings_data/audio/{ticker}_{date}.mp3 (or .wav).
      On failure, writes a pending_replay flag to capture_state.json and exits 0
      so the calling workflow does not fail the job.

  transcribe  --ticker X [--date YYYY-MM-DD]
      Run faster-whisper (model=small, compute_type=int8, device=cpu) over the
      saved audio file.  Emits timestamped text to
      earnings_data/transcripts_raw/{ticker}_{date}.txt.

  publish  --ticker X [--date YYYY-MM-DD]
      Build a password-gated HTML page:
        transcript_{ticker}_{date}.html
      Headers:
        - MACHINE-GENERATED TRANSCRIPT disclaimer (required by editorial policy)
        - Source line: "Auto-transcribed by Whisper from the company's public
          webcast, {date}. Verify quotes against the official replay before
          publication."
        - Link back to the official replay URL (if known)
      The page is injected with auth_gate.py and committed to the repo so it
      deploys to GitHub Pages.

DISCLAIMER: transcripts produced by this script are machine-generated and
may contain errors. Verify all quotes against the official company replay
before publishing any quote in a news article.

State: earnings_data/capture_state.json
Watchlist: earnings_data/transcript_watchlist.json
"""
import argparse, json, os, re, subprocess, sys
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).parent
ED = HERE / "earnings_data"
WATCHLIST_FILE = ED / "transcript_watchlist.json"
STATE_FILE = ED / "capture_state.json"
AUDIO_DIR = ED / "audio"
TRANSCRIPTS_RAW_DIR = ED / "transcripts_raw"

sys.path.insert(0, str(HERE))
from auth_gate import inject_auth


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_watchlist():
    if not WATCHLIST_FILE.exists():
        sys.exit("capture_call.py: transcript_watchlist.json not found")
    return json.loads(WATCHLIST_FILE.read_text())


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=1))


def resolve_date(ticker, date_arg, state):
    """Return the target date string YYYY-MM-DD.
    Falls back to today if --date not given and no pending_replay entry."""
    if date_arg:
        return date_arg
    pending = state.get(ticker, {}).get("pending_replay_date")
    if pending:
        return pending
    return date.today().isoformat()


# ============ CAPTURE ============

def do_capture(ticker, date_str, watchlist, state):
    """Try to capture the earnings call audio. Returns path to audio file or None."""
    entry = watchlist.get(ticker)
    if not entry:
        log(f"ERROR: {ticker} not in watchlist")
        return None

    if entry.get("live"):
        webcast_url = entry.get("webcast_url")
        if webcast_url:
            log(f"Attempting live stream capture for {ticker} from {webcast_url}")
        else:
            log(f"WARNING: {ticker} has live=true but no webcast_url configured")
            return None
    else:
        webcast_url = entry.get("replay_url")
        if not webcast_url:
            log(f"WARNING: {ticker} has live=false and no replay_url configured. "
                f"Set replay_url in transcript_watchlist.json once the call posts.")
            state.setdefault(ticker, {})["pending_replay"] = True
            state[ticker]["pending_replay_date"] = date_str
            save_state(state)
            return None

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    out_path = AUDIO_DIR / f"{ticker}_{date_str}"

    # Try yt-dlp first (handles most webcast platforms)
    log(f"Trying yt-dlp for {ticker}...")
    yt_out = str(out_path) + ".%(ext)s"
    result = subprocess.run(
        ["yt-dlp", "-x", "--audio-format", "mp3",
         "--audio-quality", "0",
         "-o", yt_out,
         webcast_url],
        capture_output=True, text=True, timeout=7200
    )
    if result.returncode == 0:
        # Find the output file
        for ext in ["mp3", "m4a", "wav", "ogg"]:
            candidate = out_path.with_suffix(f".{ext}")
            if candidate.exists():
                log(f"yt-dlp capture succeeded: {candidate}")
                return candidate
    else:
        log(f"yt-dlp failed: {result.stderr[-500:] if result.stderr else 'no output'}")

    # Fallback: ffmpeg direct stream (HLS/MP3)
    log("Trying ffmpeg direct stream capture...")
    mp3_path = out_path.with_suffix(".mp3")
    result2 = subprocess.run(
        ["ffmpeg", "-y", "-i", webcast_url,
         "-acodec", "libmp3lame", "-ar", "16000", "-ac", "1",
         str(mp3_path)],
        capture_output=True, text=True, timeout=7200
    )
    if result2.returncode == 0 and mp3_path.exists():
        log(f"ffmpeg capture succeeded: {mp3_path}")
        return mp3_path

    log(f"ffmpeg failed: {result2.stderr[-500:] if result2.stderr else 'no output'}")
    log(f"All capture methods failed for {ticker}. Setting pending_replay flag.")
    state.setdefault(ticker, {})["pending_replay"] = True
    state[ticker]["pending_replay_date"] = date_str
    save_state(state)
    return None


# ============ TRANSCRIBE ============

def do_transcribe(ticker, date_str):
    """Run faster-whisper over the captured audio. Returns path to transcript text or None."""
    TRANSCRIPTS_RAW_DIR.mkdir(parents=True, exist_ok=True)

    # Find audio file
    audio_file = None
    for ext in ["mp3", "m4a", "wav", "ogg"]:
        candidate = AUDIO_DIR / f"{ticker}_{date_str}.{ext}"
        if candidate.exists():
            audio_file = candidate
            break
    if not audio_file:
        log(f"ERROR: No audio file found for {ticker} {date_str}")
        return None

    out_txt = TRANSCRIPTS_RAW_DIR / f"{ticker}_{date_str}.txt"
    log(f"Transcribing {audio_file} with faster-whisper (model=small, int8, cpu)...")

    try:
        from faster_whisper import WhisperModel
        model = WhisperModel("small", device="cpu", compute_type="int8")
        segments, info = model.transcribe(str(audio_file), beam_size=5)
        log(f"Detected language: {info.language} (probability {info.language_probability:.2f})")
        lines = []
        for seg in segments:
            start = int(seg.start)
            h, rem = divmod(start, 3600)
            m, s = divmod(rem, 60)
            ts = f"[{h:02d}:{m:02d}:{s:02d}]"
            lines.append(f"{ts} {seg.text.strip()}")
        out_txt.write_text("\n".join(lines), encoding="utf-8")
        log(f"Transcription complete: {out_txt} ({len(lines)} segments)")
        return out_txt
    except ImportError:
        log("ERROR: faster-whisper not installed. Run: pip install faster-whisper")
        return None
    except Exception as e:
        log(f"ERROR: transcription failed: {e}")
        return None


# ============ PUBLISH ============

DISCLAIMER_HTML = """
<div class="transcript-disclaimer" role="alert">
  <h2>Machine-generated transcript — verify all quotes before publishing</h2>
  <p>This transcript was auto-generated by <strong>OpenAI Whisper</strong> from the company's
  public earnings call webcast. It has <strong>not been reviewed or corrected</strong> by a
  human editor.</p>
  <p><strong>Editorial requirement: Verify every quote against the official company replay
  before including it in any published article.</strong> Speaker attributions, proper nouns,
  financial figures, and industry terms are especially prone to transcription errors.</p>
  <p class="source-line">Source: Auto-transcribed from the company's public webcast, {date}.
  {replay_link}</p>
</div>
"""

def do_publish(ticker, date_str, watchlist):
    """Build a password-gated HTML page with the Whisper transcript."""
    txt_file = TRANSCRIPTS_RAW_DIR / f"{ticker}_{date_str}.txt"
    if not txt_file.exists():
        log(f"ERROR: Raw transcript not found: {txt_file}")
        return None

    entry = watchlist.get(ticker, {})
    company_name = entry.get("name", ticker)
    replay_url = entry.get("replay_url") or ""
    replay_link = (
        f'<a href="{replay_url}" target="_blank" rel="noopener">Official replay ↗</a>'
        if replay_url else "(Official replay URL not configured)"
    )

    disclaimer = DISCLAIMER_HTML.format(
        date=date_str,
        replay_link=replay_link,
    )

    # Build transcript body from timestamped text
    lines = txt_file.read_text(encoding="utf-8").splitlines()
    transcript_paras = []
    for line in lines:
        if not line.strip():
            continue
        # Format: [HH:MM:SS] text
        m = re.match(r"^(\[[^\]]+\])\s*(.+)$", line)
        if m:
            ts_span = f'<span class="ts">{m.group(1)}</span>'
            transcript_paras.append(
                f'<p class="seg">{ts_span} {m.group(2)}</p>'
            )
        else:
            transcript_paras.append(f'<p class="seg">{line}</p>')

    transcript_body = "\n".join(transcript_paras) if transcript_paras else "<p>No transcript text.</p>"

    page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{company_name} ({ticker}) Earnings Call Transcript — {date_str}</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif;
           margin: 0; background: #f4f5f7; color: #1a1a2e; }}
    .container {{ max-width: 900px; margin: 24px auto; padding: 0 24px 60px; }}
    h1 {{ font-size: 22px; margin: 0 0 6px; }}
    .meta {{ color: #6c757d; font-size: 13px; margin-bottom: 20px; }}
    .transcript-disclaimer {{
      background: #fff4d6; border-left: 4px solid #f59e0b;
      padding: 16px 20px; border-radius: 0 6px 6px 0; margin-bottom: 28px;
    }}
    .transcript-disclaimer h2 {{ font-size: 15px; margin: 0 0 10px; color: #92400e; }}
    .transcript-disclaimer p {{ font-size: 13.5px; margin: 6px 0; color: #78350f; line-height: 1.5; }}
    .source-line {{ font-style: italic; }}
    .transcript-body {{ background: white; padding: 28px 32px; border-radius: 6px;
                        box-shadow: 0 1px 4px rgba(0,0,0,0.08); }}
    .seg {{ font-size: 14px; line-height: 1.7; margin: 4px 0; }}
    .ts {{ color: #6c757d; font-size: 11px; font-family: ui-monospace, monospace;
           margin-right: 8px; }}
    @media (max-width: 320px) {{
      .container {{ padding: 0 12px 40px; }}
      .transcript-body {{ padding: 16px; }}
    }}
  </style>
</head>
<body>
<div class="container">
  <h1>{company_name} ({ticker}) Earnings Call — {date_str}</h1>
  <p class="meta">Machine-generated transcript. Updated {datetime.now().strftime("%Y-%m-%d %H:%M ET")}.</p>
  {disclaimer}
  <div class="transcript-body">
    {transcript_body}
  </div>
</div>
</body>
</html>"""

    out_html = HERE / f"transcript_{ticker}_{date_str}.html"
    out_html.write_text(inject_auth(page_html))
    log(f"Published: {out_html}")
    return out_html


# ============ WINDOW CHECK (unit-testable) ============

def call_in_window(call_time_et_str, run_hour_utc, window_hours=2):
    """Return True if a call at call_time_et_str falls within window_hours
    after run_hour_utc (converted to ET, EDT = UTC-4).

    Used for dry-running the scheduler logic.

    Args:
        call_time_et_str: "HH:MM ET" e.g. "08:30 ET"
        run_hour_utc: integer hour (0-23) UTC
        window_hours: how many hours after run start to cover (default 2)

    Returns True if call_hour_et is in [run_hour_et, run_hour_et + window_hours).
    """
    m = re.match(r"(\d{1,2}):(\d{2})", call_time_et_str or "")
    if not m:
        return False
    call_hour = int(m.group(1))

    # EDT is UTC-4; EST is UTC-5. We use -4 (conservative for summer).
    run_hour_et = (run_hour_utc - 4) % 24
    return run_hour_et <= call_hour < run_hour_et + window_hours


def check_windows():
    """Verify the scheduler window math (gate 5 from the plan)."""
    # 11:00 UTC run = 7:00 AM ET → covers 07:00–08:59 ET (morning calls, e.g. 08:30)
    # 17:00 UTC run = 1:00 PM ET → covers 13:00–14:59 ET (afternoon calls, e.g. 16:00)
    # But the plan says: morning covers calls through ~1 PM ET, afternoon covers the rest.
    # Let's use 6-hour windows centered on the two runs.
    test_cases = [
        # (call_time, run_utc_hour, window_hours, expected, description)
        ("08:30 ET", 11, 6, True,  "08:30 ET call at 11:00 UTC (7 AM ET) run, 6h window → True"),
        ("16:00 ET", 17, 6, True,  "16:00 ET call at 17:00 UTC (1 PM ET) run, 6h window → True"),
        ("08:30 ET", 17, 6, False, "08:30 ET call NOT covered by 17:00 UTC run, 6h window → False"),
        ("16:00 ET", 11, 6, False, "16:00 ET call NOT covered by 11:00 UTC run, 6h window → False"),
    ]
    all_pass = True
    for call_time, run_utc, window, expected, desc in test_cases:
        result = call_in_window(call_time, run_utc, window)
        status = "PASS" if result == expected else "FAIL"
        if result != expected:
            all_pass = False
        print(f"  [{status}] {desc}")
    return all_pass


# ============ MAIN ============

def main():
    parser = argparse.ArgumentParser(description="Earnings call capture and transcription")
    sub = parser.add_subparsers(dest="command")

    cap_p = sub.add_parser("capture", help="Capture earnings call audio")
    cap_p.add_argument("--ticker", required=True)
    cap_p.add_argument("--date", help="YYYY-MM-DD (defaults to today or pending_replay_date)")

    trx_p = sub.add_parser("transcribe", help="Transcribe captured audio with Whisper")
    trx_p.add_argument("--ticker", required=True)
    trx_p.add_argument("--date")

    pub_p = sub.add_parser("publish", help="Build gated HTML page from raw transcript")
    pub_p.add_argument("--ticker", required=True)
    pub_p.add_argument("--date")

    win_p = sub.add_parser("check-windows", help="Dry-run scheduler window math (gate 5)")

    args = parser.parse_args()

    if args.command == "check-windows":
        log("Checking scheduler window math...")
        ok = check_windows()
        sys.exit(0 if ok else 1)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    watchlist = load_watchlist()
    state = load_state()

    if args.command in ("capture", "transcribe", "publish"):
        date_str = resolve_date(args.ticker, getattr(args, "date", None), state)

    if args.command == "capture":
        result = do_capture(args.ticker, date_str, watchlist, state)
        if result:
            log(f"Capture complete: {result}")
            # Clear pending flag on success
            state.get(args.ticker, {}).pop("pending_replay", None)
            save_state(state)
        else:
            log(f"Capture did not produce audio for {args.ticker} {date_str}")
        # Exit 0 always — pending flag was written if needed

    elif args.command == "transcribe":
        result = do_transcribe(args.ticker, date_str)
        if not result:
            sys.exit(1)

    elif args.command == "publish":
        result = do_publish(args.ticker, date_str, watchlist)
        if not result:
            sys.exit(1)


if __name__ == "__main__":
    main()
