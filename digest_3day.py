#!/usr/bin/env python3
"""Unsourced-company signal monitor (3-day cadence).

For the companies we WANT to track but currently can't find a source for
(roster: active:false, no sources), re-probe every ~3 days:
  - Yahoo Finance search by name (catches a company that IPOs / gets a ticker),
  - a PR Newswire per-company page guessed from the name (catches a wire feed), and
  - a full-text news search (Google News RSS) restricted to a SOURCE_WHITELIST of
    substantive business/legal/local outlets (Bloomberg, FT, Inquirer, Technical.ly,
    Law360, …), deduped by title. Full-text is the only way to reach coverage of
    private/no-ticker firms (e.g. the Susquehanna insider-trading story that Yahoo's
    ticker index can't surface); the whitelist keeps it to real signal, not sports
    scores or generic-name collisions.
Email Av — and ONLY Av — when one or more shows recent signal, so we know to go
wire it in. If nothing shows signal, no email.

--live actually sends; without it, logs what it would send.
Trial: stood up 2026-07-19, revisit ~2026-08-19.
"""
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parents[2]))  # repo-root fallback (dev tree)
from email_utils import send_email
from poll_news_feed import (fetch_json, _relevant, EXCLUDED_PUBLISHERS,
                            _curl, PRN_CARD, _clean, _parse_prn_date)
import json

HERE = Path(__file__).parent
ROSTER_FILE = HERE / "earnings_data" / "local_roster.json"
SIGNAL_DAYS = 21          # only count recent activity as "signal"
TO = "agutman@inquirer.com"

# Full-text news search is a firehose. Restrict it to substantive business /
# legal / local outlets so we get real signal, not sports scores or generic-name
# collisions. Editable — matched as a lowercase substring of the item's source.
SOURCE_WHITELIST = {
    "bloomberg", "reuters", "wall street journal", "financial times", "cnbc",
    "fortune", "forbes", "associated press", "axios", "the information", "pymnts",
    "american banker", "modern healthcare", "marketwatch", "barron",
    "law360", "claims journal", "insurance journal", "the deal",
    "philadelphia inquirer", "inquirer.com", "inquirer", "technical.ly",
    "philadelphia business journal", "whyy", "billy penn",
}


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", file=sys.stderr)


def _fmt(unix):
    return datetime.fromtimestamp(unix, tz=timezone.utc).strftime("%b %-d") if unix else ""


def _slug(name):
    s = name.lower().replace("&", "and")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def probe_yahoo(name, aliases, cutoff):
    q = urllib.parse.quote(name)
    data = fetch_json(f"https://query1.finance.yahoo.com/v1/finance/search?q={q}&newsCount=15&quotesCount=3")
    if not data:
        return []
    names = [name] + aliases
    out = []
    for it in data.get("news", []):
        title = it.get("title", "")
        if not title or it.get("publisher", "") in EXCLUDED_PUBLISHERS:
            continue
        if it.get("providerPublishTime", 0) < cutoff:
            continue
        if _relevant(title, "", names):   # no ticker — match on name only
            out.append((title, it.get("publisher", ""), it.get("link", ""), it.get("providerPublishTime", 0)))
    return out


def _norm(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def probe_gnews(name, cutoff):
    """Full-text news search (Google News RSS) — reaches coverage of companies
    that have no ticker/wire (e.g. private firms). Deduped by normalized title
    so the same story from N outlets collapses to one."""
    q = urllib.parse.quote(f'"{name}"')
    txt = _curl(f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en")
    if not txt:
        return []
    seen, out = set(), []
    for block in re.findall(r"<item>(.*?)</item>", txt, re.S):
        tm = re.search(r"<title>(.*?)</title>", block, re.S)
        dm = re.search(r"<pubDate>(.*?)</pubDate>", block)
        lm = re.search(r"<link>(.*?)</link>", block)
        sm = re.search(r"<source[^>]*>(.*?)</source>", block, re.S)
        if not tm:
            continue
        title = _clean(tm.group(1))
        src = _clean(sm.group(1)) if sm else ""
        title = re.sub(r"\s*-\s*" + re.escape(src) + r"\s*$", "", title) if src else title
        if not any(w in src.lower() for w in SOURCE_WHITELIST):
            continue
        try:
            ts = int(parsedate_to_datetime(dm.group(1)).timestamp()) if dm else 0
        except Exception:
            ts = 0
        if ts and ts < cutoff:
            continue
        key = _norm(title)[:80]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append((title, src, _clean(lm.group(1)) if lm else "", ts))
    out.sort(key=lambda x: x[3], reverse=True)
    return out


def probe_prn(name, cutoff):
    url = f"https://www.prnewswire.com/news/{_slug(name)}/"
    html = _curl(url)
    if not html:
        return None
    recent = [(_clean(m.group("title")), _parse_prn_date(m.group("date")))
              for m in PRN_CARD.finditer(html)]
    recent = [(t, d) for t, d in recent if t and d >= cutoff]
    if recent:
        recent.sort(key=lambda x: x[1], reverse=True)
        return {"url": url, "count": len(recent), "latest": recent[0]}
    return None


def main():
    live = "--live" in sys.argv
    roster = json.loads(ROSTER_FILE.read_text())
    targets = [e for e in roster.get("entities", [])
               if not e.get("active") and not e.get("sources")
               and not e.get("note", "").startswith("EXCLUDED")]
    cutoff = time.time() - SIGNAL_DAYS * 86400
    log(f"Probing {len(targets)} unsourced companies for signal (last {SIGNAL_DAYS}d)")

    signals = []
    for e in targets:
        name = e["name"]
        y = probe_yahoo(name, e.get("aliases", []), cutoff)
        time.sleep(0.2)
        p = probe_prn(name, cutoff)
        time.sleep(0.2)
        g = probe_gnews(name, cutoff)
        time.sleep(0.2)
        if y or p or g:
            signals.append({"name": name, "yahoo": y, "prn": p, "gnews": g})
            log(f"  SIGNAL: {name}  yahoo={len(y)} prn={p['count'] if p else 0} gnews={len(g)}")

    if not signals:
        log("No signal for any unsourced company — no email.")
        return

    rows = ""
    for s in signals:
        bits = []
        if s["prn"]:
            t, d = s["prn"]["latest"]
            bits.append(f'<div style="margin:3px 0;">PR Newswire page found (<a href="{s["prn"]["url"]}" '
                        f'style="color:#1a5276;">{s["prn"]["count"]} recent</a>) — latest: '
                        f'"{t}" ({_fmt(d)})</div>')
        for (t, pub, link, ts) in s["yahoo"][:3]:
            bits.append(f'<div style="margin:3px 0;">Yahoo: <a href="{link}" style="color:#1a5276;">{t}</a> '
                        f'<span style="color:#888;font-size:12px;">— {pub}, {_fmt(ts)}</span></div>')
        for (t, src, link, ts) in s.get("gnews", [])[:4]:
            bits.append(f'<div style="margin:3px 0;">News: <a href="{link}" style="color:#1a5276;">{t}</a> '
                        f'<span style="color:#888;font-size:12px;">— {src}, {_fmt(ts)}</span></div>')
        rows += (f'<div style="margin:14px 0;"><div style="font-weight:700;color:#0a1628;">{s["name"]}</div>'
                 f'{"".join(bits)}</div>')

    body = f"""<html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;color:#1a1a1a;max-width:640px;">
<div style="background:#1a5c3a;color:#fff;padding:16px 20px;border-radius:8px 8px 0 0;">
  <div style="font-size:12px;letter-spacing:.5px;opacity:.8;">LOCAL FEED · SOURCE-WATCH (every 3 days)</div>
  <div style="font-size:16px;font-weight:700;margin-top:4px;">
    Signal for {len(signals)} company(ies) we want to track but couldn't source — consider wiring them in.</div>
</div>
<div style="padding:18px 20px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;">
  {rows}
  <div style="margin-top:18px;font-size:11.5px;color:#999;">
    These are roster companies with no source yet; a hit here means recent coverage turned up via Yahoo, PR Newswire,
    or a full-text news search (deduped). Verify against source material before adding. One-month trial.
  </div>
</div></body></html>"""
    subject = f"Source-watch: signal for {len(signals)} untracked compan(y/ies)"
    if live:
        ok = send_email(subject, body, log_fn=log, to=TO)
        log(f"sent={ok} to {TO}: {subject}")
    else:
        log(f"[dry-run] would send to {TO}: {subject}")


if __name__ == "__main__":
    main()
