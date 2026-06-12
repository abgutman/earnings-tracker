#!/usr/bin/env python3
"""Test suite for poll_upcoming.py extraction fixes.

Run: python3 business/earnings/test_extract_upcoming.py
All assertions must pass — no pytest required.
"""
import sys, os
from pathlib import Path

# Make sure we can import from this directory
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))

from poll_upcoming import extract_from_body, normalize_time

FIXTURE = HERE / "earnings_data" / "fixtures" / "cmcsa_2026-06-11.html"
# Article publish unix: 1781181000 (June 11 2026 ~14:30 UTC)
PUBLISH_UNIX = 1781181000

HEADER_ONLY = (
    "Comcast to Host Second Quarter 2026 Earnings Conference Call "
    "Business Wire Thu, June 11, 2026 at 5:30 AM PDT 1 min read"
)

passed = 0
failed = 0

def ok(name):
    global passed
    passed += 1
    print(f"  PASS  {name}")

def fail(name, detail):
    global failed
    failed += 1
    print(f"  FAIL  {name}: {detail}")

# ── Test 1: fixture extraction ────────────────────────────────────────────────
print("Test 1: fixture HTML extraction returns July 23 / 08:30 ET")
html_text = FIXTURE.read_text(encoding="utf-8", errors="replace")
result = extract_from_body(html_text, PUBLISH_UNIX)
if result is None:
    fail("1a call_date", f"extract_from_body returned None")
else:
    if result.get("call_date") == "2026-07-23":
        ok("1a call_date == 2026-07-23")
    else:
        fail("1a call_date", f"got {result.get('call_date')!r}, expected '2026-07-23'")

    if result.get("call_time") == "08:30 ET":
        ok("1b call_time == 08:30 ET")
    else:
        fail("1b call_time", f"got {result.get('call_time')!r}, expected '08:30 ET'")

# ── Test 2: header-only text returns no call_date ─────────────────────────────
print("Test 2: header-only text yields no call_date")
result2 = extract_from_body(HEADER_ONLY, PUBLISH_UNIX)
if result2 is None or not result2.get("call_date"):
    ok("2a no call_date from byline-only text")
else:
    fail("2a no call_date from byline-only text", f"got call_date={result2.get('call_date')!r}")

# ── Test 3: normalize_time does not stamp wrong timezone as ET ────────────────
print("Test 3: normalize_time('5:30 AM PDT') does not return an unconverted ET-labelled time")
result3 = normalize_time("5:30 AM PDT")
# It should NOT return "05:30 ET" (the old bug)
# Acceptable: return a converted value like "08:30 ET", OR keep original label
if result3 == "05:30 ET":
    fail("3a normalize_time PDT not silently relabelled as ET", f"got {result3!r}")
else:
    ok(f"3a normalize_time('5:30 AM PDT') = {result3!r} (not '05:30 ET')")

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\nResults: {passed} passed, {failed} failed")
if failed:
    print("OVERALL: FAIL")
    sys.exit(1)
else:
    print("OVERALL: PASS")
    sys.exit(0)
