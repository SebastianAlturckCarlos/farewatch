#!/usr/bin/env python3
"""
gflights.py - read real Google Flights fares with a headless browser.

Why this exists
---------------
`fast-flights` builds a protobuf query and reads the HTML Google returns. That
works for plenty of routes, but for MCI->UVF Google returns a 1.8 MB page with
zero prices in it: the results are rendered client-side by JavaScript, so
there is nothing in the HTML to parse. The same query in a real browser shows
a full board of through-fares. No HTTP-only scraper can close that gap --
not fast-flights, not a different library, not a hand-rolled request. The page
has to execute JS.

So this module drives headless Chromium and reads the results off the rendered
page. It is slower (~10-20s a query against ~2s), which is why it is only used
where it earns that: the routes fast-flights cannot see.

What it reads
-------------
Each result carries an aria-label that is a complete, human-readable summary:

    "From 2179 US dollars round trip total. 1 stop flight with American.
     Leaves Kansas City International Airport at 5:00 AM on Monday, June 14
     and arrives at Hewanorra International Airport at 1:58 PM on Monday,
     June 14. Total duration 7 hr 58 min. Layover (1 of 1) is a 1 hr 40 min
     layover at Miami International Airport in Miami. Carbon emissions
     estimate: 435 kilograms. -26% emissions. Select flight"

That is a far more stable contract than Google's obfuscated CSS classes, which
change without notice. Accessibility labels do not, because screen readers
depend on them.

    python3 gflights.py            # print what it sees for the active trips
"""

import re
import sys
from datetime import datetime

BASE = "https://www.google.com/travel/flights"

# Google names airports in prose; we need codes. Only the ones our trips touch
# have to be here -- anything unknown falls back to a trimmed name, which is
# still readable, just not a code.
CODES = {
    "Kansas City International": "MCI",
    "Hewanorra International": "UVF",
    "Miami International": "MIA",
    "Charlotte Douglas International": "CLT",
    "Hartsfield-Jackson Atlanta International": "ATL",
    "John F. Kennedy International": "JFK",
    "Chicago O'Hare International": "ORD",
    "Philadelphia International": "PHL",
    "Dallas/Fort Worth International": "DFW",
    "Punta Cana International": "PUJ",
    "Fort Lauderdale-Hollywood International": "FLL",
    "LaGuardia": "LGA",
}


def _code(name):
    name = (name or "").strip()
    for full, code in CODES.items():
        if full.lower() in name.lower():
            return code
    # "Foo International Airport" -> "Foo"
    return re.sub(r"\s+(International\s+)?Airport$", "", name).strip() or name


def _minutes(text):
    """'7 hr 58 min' -> 478."""
    if not text:
        return None
    h = re.search(r"(\d+)\s*hr", text)
    m = re.search(r"(\d+)\s*min", text)
    if not h and not m:
        return None
    return (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)


def _stamp(time_text, date_text, year):
    """('5:00 AM', 'Monday, June 14', 2027) -> '2027-06-14T05:00'."""
    if not time_text or not date_text:
        return None
    date_text = re.sub(r"^\w+day,\s*", "", date_text.strip())
    for fmt in ("%B %d %Y %I:%M %p", "%b %d %Y %I:%M %p"):
        try:
            return datetime.strptime(
                f"{date_text} {year} {time_text.strip()}", fmt
            ).isoformat(timespec="minutes")
        except ValueError:
            continue
    return None


# Parsed as independent fields rather than one long pattern. Google splices
# extra sentences into these labels without warning ("Operated by SkyWest
# Airlines as American Eagle.", "Separate tickets booked together", ...), and a
# single monolithic regex breaks the moment one appears. Each field found on
# its own is unbothered by anything between.
PRICE_RE = re.compile(r"From\s+([\d,]+)\s+US dollars")
STOPS_RE = re.compile(r"(?:(Nonstop)|(\d+)\s+stops?)\s+flight")
AIRLINE_RE = re.compile(r"(?:Nonstop|\d+\s+stops?)\s+flight\s+with\s+([^.]+?)\.")
LEAVES_RE = re.compile(
    r"Leaves\s+(?P<from>.+?)\s+at\s+(?P<dep_time>\d{1,2}:\d{2}\s*[AP]M)"
    r"\s+on\s+(?P<dep_date>[^,]+,\s*\w+\s+\d{1,2})"
    r"\s+and\s+arrives\s+at\s+(?P<to>.+?)\s+at\s+(?P<arr_time>\d{1,2}:\d{2}\s*[AP]M)"
    r"\s+on\s+(?P<arr_date>[^,]+,\s*\w+\s+\d{1,2})", re.S)
LAYOVER_RE = re.compile(
    r"Layover\s+\(\d+ of \d+\)\s+is\s+an?\s+(?P<dur>[\d]+(?:\s*hr)?(?:\s*\d+\s*min)?"
    r"|\d+\s*min|\d+\s*hr(?:\s*\d+\s*min)?)\s+layover\s+at\s+(?P<at>.+?)\s+in\s+", re.S)
DURATION_RE = re.compile(r"Total duration\s+(\d+(?:\s*hr)?(?:\s*\d+\s*min)?|\d+\s*min)\.")
CARBON_RE = re.compile(r"Carbon emissions estimate:\s*([\d,]+)\s*kilograms")


def _clean(text):
    r"""Collapse whitespace, including the exotic kinds.

    Google pads times with U+202F (narrow no-break space) and U+00A0, and
    strptime rejects both, so "5:00 PM" never parses until they are
    normalised to a plain space.
    """
    return re.sub(r"[  \s]+", " ", text or "").strip()


def parse_label(label, year):
    """Turn one result's aria-label into the same shape farewatch stores."""
    label = _clean(label)

    pm = PRICE_RE.search(label)
    lm = LEAVES_RE.search(label)
    sm = STOPS_RE.search(label)
    if not (pm and lm and sm):
        return None

    g = lm.groupdict()
    dep = _stamp(g["dep_time"], g["dep_date"], year)
    arr = _stamp(g["arr_time"], g["arr_date"], year)
    stops = 0 if sm.group(1) else int(sm.group(2))

    layovers = [
        {"at": _code(m.group("at")),
         "minutes": _minutes(m.group("dur")),
         "label": m.group("dur").strip()}
        for m in LAYOVER_RE.finditer(label)
    ]

    dm = DURATION_RE.search(label)
    total = _minutes(dm.group(1)) if dm else None
    cm = CARBON_RE.search(label)
    am = AIRLINE_RE.search(label)

    # Google gives the endpoints and the layover airports but not each hop's
    # own times, so segments are the chain of airports. First departure and
    # last arrival are exact; intermediate stamps stay None rather than
    # being invented.
    chain = [_code(g["from"])] + [l["at"] for l in layovers] + [_code(g["to"])]
    segments = [
        {"from": a, "to": b,
         "dep": dep if i == 0 else None,
         "arr": arr if i == len(chain) - 2 else None,
         "minutes": None, "plane": None}
        for i, (a, b) in enumerate(zip(chain, chain[1:]))
    ]

    return {
        "airline": (am.group(1).strip() if am else None) or None,
        "stops": stops,
        "depart_at": dep,
        "arrive_at": arr,
        "total_minutes": total,
        "total_label": f"{total // 60}h {total % 60:02d}m" if total else None,
        "segments": segments,
        "layovers": layovers,
        "carbon": int(cm.group(1).replace(",", "")) * 1000 if cm else None,
        "price": float(pm.group(1).replace(",", "")),
        "source": "google-flights",
    }


def _attempt(url, timeout_ms, verbose):
    """One browser session. Returns the raw aria-labels, or None on failure."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(
                locale="en-US",
                timezone_id="America/Chicago",
                viewport={"width": 1280, "height": 900},
            )
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            # state="attached", not the default "visible": the first matching
            # node is an offscreen price chip, so waiting for visibility times
            # out on a page that has in fact already rendered.
            page.wait_for_selector('[aria-label*="US dollars"]',
                                   state="attached", timeout=timeout_ms)
            # The board paints progressively -- the first labels can appear
            # before the full result set does. Let it settle.
            page.wait_for_timeout(1500)
            return page.eval_on_selector_all(
                '[aria-label*="US dollars"]',
                "els => els.map(e => e.getAttribute('aria-label'))")
        finally:
            browser.close()


def search(origin, dest, depart, ret, adults, timeout_ms=45000, verbose=True,
           attempts=3):
    """Return every priced itinerary Google shows, cheapest first.

    Prices are the round-trip total for all passengers -- Google's board says
    "Prices include required taxes + fees for N adults" -- which is the same
    basis farewatch stores.

    Retried, because it is genuinely flaky: perhaps one run in four the board
    does not finish rendering inside the timeout. A single failed attempt used
    to fall through to the leg-summed estimate, which quietly wrote a number
    from a different methodology into the middle of a series of real fares.
    A retry is much cheaper than a corrupted history.
    """
    try:
        import playwright  # noqa: F401
    except ImportError:
        if verbose:
            print("    [gflights] playwright not installed "
                  "(pip install playwright && python -m playwright install chromium)")
        return []

    q = (f"Flights from {origin} to {dest} on {depart} "
         f"returning {ret} {adults} adults")
    url = f"{BASE}?q={q.replace(' ', '%20')}&curr=USD&hl=en-US&gl=US"
    year = int(depart[:4])

    labels = []
    for i in range(1, attempts + 1):
        try:
            labels = _attempt(url, timeout_ms, verbose) or []
        except Exception as e:
            if verbose:
                print(f"    [gflights] attempt {i}/{attempts}: "
                      f"{type(e).__name__}")
            labels = []
        if any(l and "round trip total" in l for l in labels):
            break
        if i < attempts and verbose:
            print(f"    [gflights] attempt {i}/{attempts} returned nothing, "
                  f"retrying")
    else:
        if verbose:
            print("    [gflights] no results after "
                  f"{attempts} attempts (consent page or bot check?)")
        return []

    seen = set()
    results = []
    for label in labels:
        if not label or "round trip total" not in label:
            continue
        itin = parse_label(label, year)
        if not itin:
            continue
        key = (itin["price"], itin["depart_at"], itin["stops"])
        if key in seen:
            continue
        seen.add(key)
        results.append(itin)

    results.sort(key=lambda r: r["price"])
    return results


def main():
    import farewatch as fw
    for trip in (t for t in fw.TRIPS if t.get("enabled")):
        print(f"\n{trip['origin']} -> {trip['dest']}  "
              f"{trip['depart']} to {trip['return']}  "
              f"{trip['adults']} adults")
        rows = search(trip["origin"], trip["dest"], trip["depart"],
                      trip["return"], trip["adults"])
        if not rows:
            print("  nothing returned")
            continue
        for r in rows[:10]:
            via = ", ".join(l["at"] for l in r["layovers"]) or "nonstop"
            dep = r["depart_at"] and r["depart_at"][11:]
            arr = r["arrive_at"] and r["arrive_at"][11:]
            overnight = ""
            if r["depart_at"] and r["arrive_at"]:
                overnight = "+1" if r["arrive_at"][:10] != r["depart_at"][:10] else ""
            print(f"  ${r['price']:>7,.0f}  {r['stops']}st {via:<12} "
                  f"{dep}->{arr}{overnight:<2} {r['total_label'] or '':>8}  "
                  f"{r['airline'] or ''}")


if __name__ == "__main__":
    main()
