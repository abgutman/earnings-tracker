#!/usr/bin/env python3
"""Compute earnings watch-windows from EDGAR using fiscal-quarter anchors.

For each company, the key data we extract:

  PER QUARTER (last 4 quarters available):
    period_end   = fiscal quarter end (from EDGAR `reportDate` field)
    days_to_10q  = (10-Q/10-K filing date) - period_end
    days_to_8k   = (8-K with item 2.02 filing date) - period_end, if present
    days_to_press = same as 8-K item 2.02 for now (press release filed same day as 8-K wrapper)

  AGGREGATE:
    min/median/max of each days-to-X across the last 4 quarters
    -> next watch window for each filing type

  TODAY:
    days since last period_end
    status: "before window" | "in window" | "filed" | "overdue"

Output: windows.json
"""
import json, sys
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from statistics import median

HERE = Path(__file__).parent

def parse_d(s):
    if not s: return None
    try: return datetime.fromisoformat(s).date()
    except: return None

def load_filings(sub):
    """Return all recent filings with (form, filingDate, reportDate, items) parsed."""
    rec = sub.get("filings",{}).get("recent",{}) or {}
    out = []
    n = len(rec.get("form", []))
    for i in range(n):
        out.append({
            "form": rec["form"][i],
            "filingDate": parse_d(rec["filingDate"][i]),
            "reportDate": parse_d((rec.get("reportDate") or [None]*n)[i]),
            "items": (rec.get("items") or [""]*n)[i] or "",
            "accession": (rec.get("accessionNumber") or [""]*n)[i],
            "primaryDoc": (rec.get("primaryDocument") or [""]*n)[i],
        })
    return out

def compute_company_window(sub):
    """For one company submissions JSON, build the window record."""
    filings = load_filings(sub)
    if not filings: return None

    # --- 10-Q / 10-K history (most recent quarters first) ---
    quarterly = [f for f in filings if f["form"] in ("10-Q","10-Q/A","10-K","10-K/A","20-F","20-F/A")
                                   and f["filingDate"] and f["reportDate"]]
    # Keep only last 6 quarters of data
    quarterly = quarterly[:6]

    # Compute days_to_filing per quarter
    q_records = []
    for f in quarterly:
        gap = (f["filingDate"] - f["reportDate"]).days
        if 5 < gap < 180:  # sanity filter
            q_records.append({
                "form": f["form"],
                "period_end": f["reportDate"].isoformat(),
                "filed": f["filingDate"].isoformat(),
                "days_after": gap,
                "accession": f["accession"],
                "primaryDoc": f["primaryDoc"],
            })

    # --- 8-K item 2.02 history mapped to a quarter ---
    earn_8ks = [f for f in filings if f["form"] == "8-K" and "2.02" in (f["items"] or "")
                                  and f["filingDate"]]
    # Map each earn 8-K to the most recent prior period_end from our 10-Qs+10-Ks
    period_ends = sorted({f["reportDate"] for f in quarterly if f["reportDate"]}, reverse=True)
    eight_k_records = []
    for ek in earn_8ks[:6]:
        # Find the period_end this 8-K is reporting on (most recent period_end <= filing date)
        match = None
        for pe in period_ends:
            if pe <= ek["filingDate"] and (ek["filingDate"] - pe).days < 180:
                match = pe; break
        if not match: continue
        gap = (ek["filingDate"] - match).days
        if 5 < gap < 180:
            eight_k_records.append({
                "period_end": match.isoformat(),
                "filed": ek["filingDate"].isoformat(),
                "days_after": gap,
                "accession": ek["accession"],
                "primaryDoc": ek["primaryDoc"],
            })

    # --- Aggregate stats ---
    def stats(records, n=4):
        gaps = [r["days_after"] for r in records[:n]]
        if not gaps: return None
        return {
            "n_samples": len(gaps),
            "min": min(gaps),
            "max": max(gaps),
            "median": int(median(gaps)),
            "samples": gaps,
        }

    q_stats = stats(q_records, n=4)
    e_stats = stats(eight_k_records, n=4)

    # --- Project next quarter's window ---
    latest_period = max((parse_d(r["period_end"]) for r in q_records), default=None)
    next_period = None
    if latest_period:
        # Next quarter ends ~91 days later (simplistic, but fine — companies' actual quarter-end pattern is steady)
        next_period = latest_period + timedelta(days=91)

    def window(period_end, st):
        if period_end is None or st is None: return None
        return {
            "period_end": period_end.isoformat() if hasattr(period_end, "isoformat") else period_end,
            "window_start": (period_end + timedelta(days=st["min"])).isoformat(),
            "window_end": (period_end + timedelta(days=st["max"])).isoformat(),
            "median_date": (period_end + timedelta(days=st["median"])).isoformat(),
        }

    today = date.today()
    last_q_window = window(latest_period, q_stats) if (latest_period and q_stats) else None
    last_e_window = window(latest_period, e_stats) if (latest_period and e_stats) else None
    next_q_window = window(next_period, q_stats) if (next_period and q_stats) else None
    next_e_window = window(next_period, e_stats) if (next_period and e_stats) else None

    # Status based on next_q_window
    status = "unknown"
    if next_q_window:
        ws = datetime.fromisoformat(next_q_window["window_start"]).date()
        we = datetime.fromisoformat(next_q_window["window_end"]).date()
        if today < ws:
            status = "before_window"
            days_until = (ws - today).days
        elif today > we:
            status = "overdue"
            days_until = -(today - we).days
        else:
            status = "in_window"
            days_until = 0
    else:
        days_until = None

    # Determine filing-time-of-day pattern from 8-K acceptance times (A/B/C/D)
    accept_times_et = []
    rec = sub.get("filings",{}).get("recent",{}) or {}
    for i in range(len(rec.get("form", []))):
        if rec["form"][i] == "8-K" and "2.02" in (rec.get("items") or [""])[i]:
            try:
                dt_utc = datetime.fromisoformat(rec["acceptanceDateTime"][i].replace("Z","+00:00"))
                dt_et = dt_utc.astimezone(timezone(timedelta(hours=-4)))
                accept_times_et.append(dt_et.time())
            except: pass
    pattern_label = "unknown"
    median_time = None
    if accept_times_et:
        # Use most recent 4 filings
        recent_times = accept_times_et[:4]
        avg_minute = int(median([t.hour*60 + t.minute for t in recent_times]))
        h = avg_minute // 60
        m = avg_minute % 60
        median_time = f"{h:02d}:{m:02d}"
        if avg_minute < 9*60 + 30:
            pattern_label = "A (pre-market)"
        elif avg_minute >= 16*60:
            pattern_label = "B/C (post-market)"
        elif 9*60+30 <= avg_minute < 16*60:
            pattern_label = "D (mid-day)"

    # Most recent earnings PRESS EVENT — this is the news date.
    # Prioritize 8-K item 2.02 (which equals the press release date for almost all filers).
    # Only fall back to 10-Q date if no 8-K item-2.02 exists for the most recent period
    # (some smaller filers skip the 8-K and just file the 10-Q).
    most_recent = None
    most_recent_10q = None
    if eight_k_records:
        ek = eight_k_records[0]
        d = parse_d(ek["filed"])
        most_recent = {
            "date": d.isoformat(),
            "form": "8-K (item 2.02 — press release)",
            "days_ago": (today - d).days,
            "primaryDoc": ek["primaryDoc"],
            "accession": ek["accession"],
            "is_news_date": True,
        }
    if q_records:
        q = q_records[0]
        d = parse_d(q["filed"])
        most_recent_10q = {
            "date": d.isoformat(),
            "form": q["form"],
            "days_ago": (today - d).days,
            "primaryDoc": q["primaryDoc"],
            "accession": q["accession"],
            "is_news_date": False,  # 10-Q is the formal filing, not the news event
        }
    # If we have no 8-K but DO have a 10-Q, the 10-Q has to be our best signal
    if most_recent is None and most_recent_10q is not None:
        most_recent = {**most_recent_10q, "form": most_recent_10q["form"] + " (no 8-K — using 10-Q as proxy)"}

    return {
        "10q_history": q_records,
        "8k_history": eight_k_records,
        "10q_stats": q_stats,
        "8k_stats": e_stats,
        "last_period_end": latest_period.isoformat() if latest_period else None,
        "next_period_end": next_period.isoformat() if next_period else None,
        "last_10q_window": last_q_window,
        "last_8k_window": last_e_window,
        "next_10q_window": next_q_window,
        "next_8k_window": next_e_window,
        "status": status,
        "days_until_window_or_overdue": days_until,
        "most_recent_earnings_filing": most_recent,
        "most_recent_10q_filing": most_recent_10q,
        "filing_time_pattern": pattern_label,
        "median_filing_time_et": median_time,
    }

def build_all():
    companies = json.loads((HERE / "expanded_companies.json").read_text())
    out = []
    cache = HERE / "submissions_cache"
    for c in companies:
        cik = c.get("cik")
        if not cik: continue
        cik_p = f"{int(cik):010d}"
        cache_file = cache / f"CIK{cik_p}.json"
        if not cache_file.exists():
            print(f"  WARN: no submissions for {c.get('name')}", file=sys.stderr)
            continue
        sub = json.loads(cache_file.read_text())
        win = compute_company_window(sub)
        if not win: continue
        # Carry over identifying fields
        ticker = (c.get("tickers") or [c.get("ticker_hint","")])[0]
        out.append({
            "cik": cik,
            "cik_padded": cik_p,
            "ticker": ticker,
            "name": c.get("name") or c.get("seed_name",""),
            "city": c.get("city",""),
            "state": c.get("state",""),
            "core_county": c.get("core_county"),
            "region_tier": c.get("region_tier","core"),
            "priority_tier": c.get("priority_tier","unknown"),
            "fiscal_year_end": sub.get("fiscalYearEnd",""),
            **win,
        })
    out_path = HERE / "windows.json"
    out_path.write_text(json.dumps(out, indent=1, default=str))
    print(f"Wrote {out_path} with {len(out)} entries", file=sys.stderr)

    # Quick check: print Passage Bio + Comcast
    for tk in ("PASG","CMCSA","TOL","CPB"):
        rec = next((r for r in out if r["ticker"] == tk), None)
        if not rec: continue
        print(f"\n=== {tk}: {rec['name']} ===")
        print(f"  Last period end: {rec['last_period_end']}")
        print(f"  10-Q stats: {rec['10q_stats']}")
        print(f"  8-K (2.02) stats: {rec['8k_stats']}")
        print(f"  Most recent: {rec['most_recent_earnings_filing']}")
        if rec.get("next_10q_window"):
            print(f"  Next 10-Q window: {rec['next_10q_window']['window_start']} to {rec['next_10q_window']['window_end']} (median {rec['next_10q_window']['median_date']})")
        if rec.get("next_8k_window"):
            print(f"  Next press window: {rec['next_8k_window']['window_start']} to {rec['next_8k_window']['window_end']} (median {rec['next_8k_window']['median_date']})")
        print(f"  Status: {rec['status']} (days {rec['days_until_window_or_overdue']})")

if __name__ == "__main__":
    build_all()
