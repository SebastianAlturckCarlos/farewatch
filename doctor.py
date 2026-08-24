#!/usr/bin/env python3
"""
doctor.py - isolate WHY no fares come back.

    python doctor.py

MCI-DEN works but MCI-UVF crashes, so the scraper is fine and something about
this route or this date returns an empty payload. This runs a matrix that
separates those two variables.
"""

import traceback
from datetime import date, timedelta

try:
    from fast_flights import get_flights, create_filter, FlightQuery, Passengers
except ImportError:
    raise SystemExit("run:  python -m pip install fast-flights")

NEAR = (date.today() + timedelta(days=60)).isoformat()
FAR = "2027-06-14"


def probe(label, origin, dest, day, adults=1):
    try:
        f = create_filter(
            flights=[FlightQuery(date=day, from_airport=origin, to_airport=dest)],
            trip="one-way", seat="economy",
            passengers=Passengers(adults=adults), currency="USD")
        res = list(get_flights(f))
        if res:
            cheapest = min((str(getattr(r, "price", "?")) for r in res), key=len)
            print(f"  OK     {label:<34} {len(res):>3} results   {cheapest}")
            return "ok"
        print(f"  EMPTY  {label:<34}   0 results")
        return "empty"
    except TypeError as e:
        if "subscriptable" in str(e):
            print(f"  EMPTY  {label:<34}   Google returned no flight data")
            return "empty"
        print(f"  ERROR  {label:<34}   {type(e).__name__}: {e}")
        return "error"
    except Exception as e:
        print(f"  ERROR  {label:<34}   {type(e).__name__}: {str(e)[:50]}")
        return "error"


print("=" * 66)
print("  Isolating route vs date")
print("=" * 66)
print(f"\n  near date = {NEAR}   far date = {FAR}\n")

r = {}
r["ctrl"] = probe("MCI-DEN  near   (control)", "MCI", "DEN", NEAR)
r["date"] = probe("MCI-DEN  Jun 2027  (date test)", "MCI", "DEN", FAR)
r["route"] = probe("MIA-UVF  Jun 2027  (route test)", "MIA", "UVF", FAR)
r["near"] = probe("MCI-UVF  near", "MCI", "UVF", NEAR)
r["real"] = probe("MCI-UVF  Jun 2027  (your trip)", "MCI", "UVF", FAR)
r["rt"] = None

print("\n" + "-" * 66)
print("  DIAGNOSIS")
print("-" * 66)

if r["ctrl"] != "ok":
    print("  Scraper can't fetch anything. Reinstall: pip install -U fast-flights")
elif r["real"] == "ok":
    print("  Your route works. The round-trip query was the problem, not the")
    print("  route -- farewatch.py will fall back to one-way pricing.")
elif r["date"] != "ok":
    print("  June 2027 returns nothing even on a busy domestic route.")
    print("  -> fast-flights can't read that far out. This is a TOOL limit,")
    print("     not a fare signal. Reinstall the newest fast-flights; if that")
    print("     doesn't help there is no free replacement -- Amadeus killed")
    print("     its Self-Service tier on 17 Jul 2026 -- so fall back to")
    print("     tracking the gateway hop (CLT/MIA/JFK -> UVF) as a proxy.")
elif r["route"] != "ok" and r["near"] == "ok":
    print("  MCI-UVF works for near dates but not June 2027, and MIA-UVF is")
    print("  also empty then. -> Airlines haven't loaded June 2027 St Lucia")
    print("  schedules into the inventory this tool reads yet. Nothing is")
    print("  broken; there is simply nothing to track right now.")
elif r["route"] == "ok":
    print("  MIA-UVF prices but MCI-UVF doesn't -> Google isn't constructing")
    print("  a connecting itinerary from MCI this far out. That is expected,")
    print("  and farewatch.py already handles it: it prices MCI->gateway and")
    print("  gateway->UVF separately, sums them, and flags the reading as an")
    print("  ESTIMATE. Nothing to fix. It stops estimating on its own the day")
    print("  a through-fare gets filed.")
else:
    print("  Mixed result. Paste this whole output and we'll work from it.")
print("-" * 66)
