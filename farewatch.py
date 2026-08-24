#!/usr/bin/env python3
"""
farewatch.py - track airfare over time and turn it into a decision, not a feeling.

Built for: MCI -> UVF (St Lucia), Jun 14-19 2027, 2 adults.
Also tracks the bachelor trip (MCI -> PUJ) if you enable it below.

What it does that a Google Flights alert does not:
  1. Keeps a full price history in SQLite so you can see the actual trend
     instead of reacting to one ping.
  2. Records the whole itinerary -- every segment, every layover, aircraft
     type, arrival time -- not just a headline number. A fare that drops $200
     by adding a 6-hour layover has not actually got cheaper.
  3. Runs a "bucket depletion test" -- prices 1 passenger and 2 passengers
     separately. If 2x(1-pax) is much cheaper than the 2-pax fare, the cheap
     fare bucket is nearly empty. That is a scarcity signal, and scarcity is
     the real risk on a thin route, not price drift.
  4. Applies your own rules (target price, hard deadline) and prints a verdict.

Usage:
    pip install fast-flights
    python3 farewatch.py            # fetch + record + report
    python3 farewatch.py --report   # report only, no network call
    python3 farewatch.py --chart    # write pricehistory.png (needs matplotlib)

Cron it daily:
    0 9 * * *  cd /path/to/dir && /usr/bin/python3 farewatch.py >> farewatch.log 2>&1
"""

import argparse
import json
import sqlite3
import statistics
import sys
from datetime import datetime, date, timedelta

# Windows consoles default to cp1252, which cannot encode the arrows and
# middots this report is built from. Fail soft rather than crash a cron job.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

try:
    from fast_flights import get_flights, create_filter, FlightQuery, Passengers
except ImportError as e:
    # Don't flatten this to "run pip install". fast-flights pulls in primp,
    # which ships per-platform binary wheels, and when one of those is missing
    # or mismatched the failure surfaces here as an ImportError that has
    # nothing to do with fast-flights being absent. Show what actually broke.
    sys.exit(f"Cannot import fast_flights: {e}\n"
             f"If the module is genuinely missing:  pip install fast-flights\n"
             f"If it is installed, a dependency it does not declare is absent "
             f"(typing_extensions) or its binary wheel (primp) did not load.")

# ----------------------------------------------------------------------------
# CONFIG - edit this block, nothing else
# ----------------------------------------------------------------------------

DB_PATH = "fares.db"

TRIPS = [
    {
        "name": "St Lucia",
        "origin": "MCI",
        "dest": "UVF",
        "depart": "2027-06-14",
        "return": "2027-06-19",
        "adults": 2,
        "target_price": 2200,      # total for all pax -- BUY at or below this
        "deadline": "2027-03-01",  # hard decide-by date
        "max_stops": 1,
        "latest_arrival_hour": 18, # daylight arrival; 90 min transfer to La Toc
        "same_day_arrival": True,  # a next-day landing costs a night of the trip
        "use_browser": True,       # read real through-fares via gflights.py
        # Google will not build MCI-UVF through-itineraries this far out -- not
        # for June 2027, and not for any 2027 date we probed. When no
        # through-fare exists we price MCI->gateway and gateway->UVF separately
        # and sum them. That is an ESTIMATE, not a bookable fare, and every
        # reading taken this way is flagged as one. It drops away on its own
        # the day Google starts constructing the full itinerary.
        "gateways": ["MIA", "CLT", "ATL", "JFK"],
        "enabled": True,
    },
    {
        "name": "Bachelor (Punta Cana)",
        "origin": "MCI",
        "dest": "PUJ",
        "depart": "2027-05-20",
        "return": "2027-05-23",
        "adults": 1,
        "target_price": 700,
        "deadline": "2027-04-01",
        "max_stops": 1,
        "latest_arrival_hour": 20,
        "same_day_arrival": True,
        "use_browser": True,
        "gateways": ["MIA", "FLL", "ATL", "CLT"],
        "enabled": False,          # flip to True if you book flights separately
    },
]

# If 2-pax fare exceeds 2 x (1-pax fare) by more than this %, the cheap bucket
# is thin. Historically anything over ~15% means seats are running out.
BUCKET_GAP_ALERT_PCT = 15.0

# Shortest connection we would actually book at a US gateway, domestic arrival
# to international departure, bags recheck included.
MIN_CONNECT_MIN = 90

# ----------------------------------------------------------------------------


NEW_COLUMNS = (
    ("stops", "INTEGER"),        # segments - 1 on the outbound
    ("duration_min", "INTEGER"), # door to door on the outbound
    ("estimate", "INTEGER"),     # 1 = summed from separate legs, not bookable
    ("gateway", "TEXT"),         # connecting airport when estimated
    ("arrive_at", "TEXT"),       # ISO arrival at destination
    ("itinerary", "TEXT"),       # full JSON: every segment and layover
    ("source", "TEXT"),          # google-flights | fast-flights tier | via-XXX
    ("cheapest_any", "REAL"),    # cheapest on the board, rules ignored
)


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TEXT NOT NULL,
            trip        TEXT NOT NULL,
            origin      TEXT,
            dest        TEXT,
            depart      TEXT,
            ret         TEXT,
            adults      INTEGER,
            total_price REAL,
            per_person  REAL,
            solo_price  REAL,
            bucket_gap  REAL,
            n_options   INTEGER,
            best_airline TEXT,
            best_duration TEXT,
            daylight_ok INTEGER,
            raw         TEXT
        )
    """)
    # Migrate in place: the detail columns were added after the first readings
    # were already recorded, so ALTER rather than recreate.
    have = {r[1] for r in conn.execute("PRAGMA table_info(observations)")}
    for col, decl in NEW_COLUMNS:
        if col not in have:
            conn.execute(f"ALTER TABLE observations ADD COLUMN {col} {decl}")
    conn.commit()


def _parse_price(p):
    """fast-flights returns price as a string like '$1,204'. Make it a number."""
    if p is None:
        return None
    if isinstance(p, (int, float)):
        return float(p)
    cleaned = "".join(ch for ch in str(p) if ch.isdigit() or ch == ".")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


# ----------------------------------------------------------------------------
# Itinerary detail
#
# fast-flights hands back the whole routing -- every segment, with airports,
# local departure and arrival stamps, leg duration and aircraft type. The old
# version of this file kept the airline string and threw the rest away, which
# is why the report could not tell a clean nonstop from a red-eye with a
# 9-hour layover at the same price. We keep all of it.
# ----------------------------------------------------------------------------

def _dt(simple):
    """fast-flights SimpleDatetime -> datetime. date=(y,m,d), time=(h,m)."""
    if simple is None:
        return None
    d = getattr(simple, "date", None)
    t = getattr(simple, "time", None) or (0, 0)
    if not d:
        return None
    try:
        return datetime(d[0], d[1], d[2], t[0], t[1])
    except (TypeError, IndexError, ValueError):
        return None


def _dur(minutes):
    """183 -> '3h 03m'."""
    if minutes is None:
        return None
    minutes = int(minutes)
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _code(airport):
    return getattr(airport, "code", None) or str(airport or "")


def itinerary(flight):
    """Everything Google will tell us about one priced itinerary."""
    if flight is None:
        return None

    segments = []
    for leg in getattr(flight, "flights", None) or []:
        dep, arr = _dt(getattr(leg, "departure", None)), _dt(getattr(leg, "arrival", None))
        segments.append({
            "from": _code(getattr(leg, "from_airport", None)),
            "to": _code(getattr(leg, "to_airport", None)),
            "dep": dep.isoformat(timespec="minutes") if dep else None,
            "arr": arr.isoformat(timespec="minutes") if arr else None,
            "minutes": getattr(leg, "duration", None),
            "plane": getattr(leg, "plane_type", None),
        })

    # Layover = gap between one segment landing and the next pushing back.
    layovers = []
    for a, b in zip(segments, segments[1:]):
        if a["arr"] and b["dep"]:
            gap = int((datetime.fromisoformat(b["dep"])
                       - datetime.fromisoformat(a["arr"])).total_seconds() // 60)
            layovers.append({"at": a["to"], "minutes": gap, "label": _dur(gap)})

    airlines = getattr(flight, "airlines", None) or []
    if not isinstance(airlines, (list, tuple)):
        airlines = [airlines]
    airline = ", ".join(
        str(getattr(a, "name", None) or a) for a in airlines if a)

    first = segments[0] if segments else {}
    last = segments[-1] if segments else {}
    total = None
    if first.get("dep") and last.get("arr"):
        total = int((datetime.fromisoformat(last["arr"])
                     - datetime.fromisoformat(first["dep"])).total_seconds() // 60)

    return {
        "airline": airline or None,
        "stops": max(0, len(segments) - 1),
        "depart_at": first.get("dep"),
        "arrive_at": last.get("arr"),
        "total_minutes": total,
        "total_label": _dur(total),
        "segments": segments,
        "layovers": layovers,
        "carbon": getattr(getattr(flight, "carbon", None), "emission", None),
    }


def summarize(itin):
    """One line a human can read: 'AA · 1 stop CLT · 09:29 → 14:10 · 4h 41m'."""
    if not itin:
        return None
    bits = []
    if itin.get("airline"):
        bits.append(itin["airline"])
    if itin["stops"] == 0:
        bits.append("nonstop")
    else:
        via = ", ".join(l["at"] for l in itin["layovers"]) or f'{itin["stops"]} stops'
        bits.append(f'{itin["stops"]} stop{"s" if itin["stops"] > 1 else ""} {via}')
    if itin.get("depart_at") and itin.get("arrive_at"):
        dep = datetime.fromisoformat(itin["depart_at"])
        arr = datetime.fromisoformat(itin["arrive_at"])
        overnight = "+1" if arr.date() > dep.date() else ""
        bits.append(f'{dep:%H:%M} → {arr:%H:%M}{overnight}')
    if itin.get("total_label"):
        bits.append(itin["total_label"])
    return " · ".join(bits)


# Progressively looser query tiers. A strict query returns nothing surprisingly
# often on thin routes -- max_stops=1 plus an arrival-time cutoff plus no basic
# economy can eliminate every result. Rather than show an empty screen, we relax
# one constraint at a time and record which tier actually produced fares.
TIERS = [
    ("strict",   dict(stops=True,  arrival=True,  no_basic=True)),
    ("no-arrival-filter", dict(stops=True,  arrival=False, no_basic=True)),
    ("any-stops",dict(stops=False, arrival=False, no_basic=True)),
    ("incl-basic", dict(stops=False, arrival=False, no_basic=False)),
]


def _attempt(trip, adults, opts):
    legs = [
        FlightQuery(
            date=trip["depart"],
            from_airport=trip["origin"],
            to_airport=trip["dest"],
            max_stops=trip.get("max_stops") if opts["stops"] else None,
            latest_arrival_hour=trip.get("latest_arrival_hour") if opts["arrival"] else None,
        ),
        FlightQuery(
            date=trip["return"],
            from_airport=trip["dest"],
            to_airport=trip["origin"],
            max_stops=trip.get("max_stops") if opts["stops"] else None,
        ),
    ]
    f = create_filter(
        flights=legs,
        trip="round-trip",
        seat="economy",
        passengers=Passengers(adults=adults),
        currency="USD",
        exclude_basic_economy=opts["no_basic"],
    )
    results = get_flights(f)
    priced = []
    for r in results:
        val = _parse_price(getattr(r, "price", None))
        if val:
            priced.append((val, r))
    priced.sort(key=lambda x: x[0])
    return priced


def _leg(origin, dest, out_date, back_date, adults, max_stops=None):
    """Price one round-trip segment. Returns (price, n_options, best_flight)."""
    legs = [FlightQuery(date=out_date, from_airport=origin, to_airport=dest,
                        max_stops=max_stops),
            FlightQuery(date=back_date, from_airport=dest, to_airport=origin,
                        max_stops=max_stops)]
    f = create_filter(flights=legs, trip="round-trip", seat="economy",
                      passengers=Passengers(adults=adults), currency="USD")
    priced = []
    for r in get_flights(f):
        v = _parse_price(getattr(r, "price", None))
        if v:
            priced.append((v, r))
    priced.sort(key=lambda x: x[0])
    return (priced[0][0], len(priced), priced[0][1]) if priced else (None, 0, None)


def fetch_composite(trip, adults, verbose=True):
    """Price MCI->gateway and gateway->UVF separately, then sum.

    Google will not build MCI-UVF through-itineraries this far out, but it
    prices both halves fine. Summing them tracks the real cost of the trip
    instead of showing an empty screen until the airlines finish filing.

    The sum is an ESTIMATE. We also check whether the two halves actually
    connect -- feeder landing plus MIN_CONNECT_MIN against the hop's pushback --
    because two separately-cheap legs that need an overnight at the gateway is
    a hotel night, not a saving. The connection status rides along with the
    reading so the app can say which it is.
    """
    best = None
    for gw in trip.get("gateways", []):
        try:
            feed_p, feed_n, feed_f = _leg(trip["origin"], gw, trip["depart"],
                                          trip["return"], adults, max_stops=0)
            hop_p, hop_n, hop_f = _leg(gw, trip["dest"], trip["depart"],
                                       trip["return"], adults, max_stops=0)
        except Exception as e:
            if verbose:
                print(f"    via {gw}: {type(e).__name__}")
            continue
        if feed_p is None or hop_p is None:
            if verbose:
                print(f"    via {gw}: incomplete "
                      f"(feeder {'ok' if feed_p else 'none'}, "
                      f"hop {'ok' if hop_p else 'none'})")
            continue

        feed_it, hop_it = itinerary(feed_f), itinerary(hop_f)
        connect = _connection(feed_it, hop_it)
        total = feed_p + hop_p
        if verbose:
            print(f"    via {gw}: ${feed_p:,.0f} + ${hop_p:,.0f} = ${total:,.0f}"
                  f"   {connect['label']}")
        cand = (total, hop_n, feed_it, hop_it, gw, connect)
        if best is None or total < best[0]:
            best = cand

    if best is None:
        return None
    total, n, feed_it, hop_it, gw, connect = best
    return {
        "total": total,
        "n_options": n,
        "tier": f"via-{gw}",
        "estimate": True,
        "gateway": gw,
        "itinerary": {
            "airline": " + ".join(
                x for x in ((feed_it or {}).get("airline"),
                            (hop_it or {}).get("airline")) if x),
            "stops": 1,
            "depart_at": (feed_it or {}).get("depart_at"),
            "arrive_at": (hop_it or {}).get("arrive_at"),
            "total_minutes": None,
            "total_label": None,
            "segments": ((feed_it or {}).get("segments") or [])
                        + ((hop_it or {}).get("segments") or []),
            "layovers": [{"at": gw, "minutes": connect["minutes"],
                          "label": connect["label"]}],
            "connection": connect,
            "carbon": None,
        },
        "note": connect["label"],
    }


def _connection(feed_it, hop_it):
    """Does the feeder actually connect to the hop, same day, with slack?"""
    arr = (feed_it or {}).get("arrive_at")
    dep = (hop_it or {}).get("depart_at")
    if not arr or not dep:
        return {"ok": None, "minutes": None, "label": "connection unknown"}
    gap = int((datetime.fromisoformat(dep)
               - datetime.fromisoformat(arr)).total_seconds() // 60)
    if gap < 0:
        return {"ok": False, "minutes": gap,
                "label": "no same-day connection (needs overnight at gateway)"}
    if gap < MIN_CONNECT_MIN:
        return {"ok": False, "minutes": gap,
                "label": f"connection too tight ({_dur(gap)})"}
    if gap > 8 * 60:
        return {"ok": True, "minutes": gap,
                "label": f"connects, but {_dur(gap)} on the ground"}
    return {"ok": True, "minutes": gap, "label": f"connects ({_dur(gap)})"}


def qualifies(itin, trip):
    """Does this itinerary meet the rules you would actually book under?

    Cheapest is not the same as best. The $1,587 board-topper on this route is
    two stops and lands at 14:10 the *next* day, which costs a night of a
    five-night trip and misses the daylight transfer to La Toc entirely. The
    fare we track is the cheapest one you would genuinely take.
    """
    if trip.get("max_stops") is not None and itin.get("stops", 0) > trip["max_stops"]:
        return False
    dep, arr = itin.get("depart_at"), itin.get("arrive_at")
    if trip.get("same_day_arrival", True) and dep and arr:
        if arr[:10] != dep[:10]:
            return False
    if arr and trip.get("latest_arrival_hour") is not None:
        if datetime.fromisoformat(arr).hour >= trip["latest_arrival_hour"]:
            return False
    return True


def fetch_browser(trip, adults, verbose=True):
    """Real through-fares, read off a rendered Google Flights board.

    This is the primary source. fast-flights cannot see this route at all --
    Google returns its results as client-side JavaScript, so the HTML has no
    prices in it no matter which library asks. See gflights.py.
    """
    try:
        import gflights
    except ImportError:
        return None

    rows = gflights.search(trip["origin"], trip["dest"], trip["depart"],
                           trip["return"], adults, verbose=verbose)
    if not rows:
        return None

    good = [r for r in rows if qualifies(r, trip)]
    pick = (good or rows)[0]
    cheapest = rows[0]["price"]

    note = None
    if not good:
        note = ("nothing meets your rules right now; showing the cheapest "
                "itinerary on the board")
    elif pick["price"] > cheapest:
        note = (f"${cheapest:,.0f} exists but breaks your rules "
                f"(extra stop or next-day arrival)")

    if verbose:
        print(f"  {len(rows)} itineraries on the board, "
              f"{len(good)} meet your rules "
              f"(<= {trip.get('max_stops')} stop, same-day, "
              f"arrives before {trip.get('latest_arrival_hour')}:00)")

    itin = dict(pick)
    itin.pop("price", None)
    return {
        "total": pick["price"],
        # Now a real scarcity signal: how many BOOKABLE options fit your rules.
        "n_options": len(good),
        "tier": "google-flights",
        "estimate": False,
        "gateway": None,
        "itinerary": itin,
        "cheapest_any": cheapest,
        "note": note,
    }


def fetch(trip, adults, verbose=True):
    """Return a reading dict, or None.

    Order matters. The browser sees real through-fares and is tried first;
    fast-flights is the quick fallback for routes it can actually read; the
    leg-by-leg estimate is the last resort and is always flagged as one.
    """
    if trip.get("use_browser", True):
        r = fetch_browser(trip, adults, verbose)
        if r:
            return r
        # Deliberately do NOT fall through to the leg-summed estimate here.
        # Once a route is known to have real through-fares, an estimate is not
        # a degraded reading of the same quantity -- it measures something
        # else (two separate tickets, nonstop legs only) and lands roughly
        # $500 high. Writing that into the middle of a real series corrupts
        # the low, the high, the percentile and every alert that reads them.
        # A gap is honest; a wrong number is not.
        if verbose:
            print("  browser returned nothing — skipping this reading rather "
                  "than recording an estimate that isn't comparable")
        return None

    last_err = None
    for name, opts in TIERS:
        try:
            priced = _attempt(trip, adults, opts)
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            continue
        if priced:
            if verbose and name != "strict":
                print(f"  (strict filters returned nothing; used tier '{name}')")
            return {
                "total": priced[0][0],
                "n_options": len(priced),
                "tier": name,
                "estimate": False,
                "gateway": None,
                "itinerary": itinerary(priced[0][1]),
                "note": None,
            }

    if trip.get("gateways"):
        if verbose:
            print("  no through-fare yet; pricing leg by leg:")
        return fetch_composite(trip, adults, verbose)

    if verbose:
        print("  ! no fares from any tier"
              + (f" -- last error {last_err}" if last_err else ""))
    return None


def record(conn, trip):
    print(f"\n>> {trip['name']}: {trip['origin']}->{trip['dest']} "
          f"{trip['depart']} to {trip['return']}")

    adults = trip["adults"]
    r = fetch(trip, adults)
    if r is None:
        print("  no results (route may have no availability loaded yet)")
        return None

    total = r["total"]
    itin = r["itinerary"] or {}

    # bucket depletion test -- only meaningful for multi-passenger trips
    solo = None
    gap = None
    if adults > 1:
        solo_r = fetch(trip, 1, verbose=False)
        solo = solo_r["total"] if solo_r else None
        if solo:
            expected = solo * adults
            gap = ((total - expected) / expected) * 100.0

    airline = itin.get("airline")
    if r["estimate"] and r.get("gateway"):
        airline = f'via {r["gateway"]}' + (f" · {airline}" if airline else "")

    arrive_at = itin.get("arrive_at")
    daylight = 0
    if arrive_at:
        arr = datetime.fromisoformat(arrive_at)
        dep_date = date.fromisoformat(trip["depart"])
        daylight = 1 if (arr.hour < trip.get("latest_arrival_hour", 24)
                         and arr.date() == dep_date) else 0

    conn.execute("""
        INSERT INTO observations (observed_at, trip, origin, dest, depart, ret,
            adults, total_price, per_person, solo_price, bucket_gap, n_options,
            best_airline, best_duration, daylight_ok, raw,
            stops, duration_min, estimate, gateway, arrive_at, itinerary,
            source, cheapest_any)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        datetime.now().isoformat(timespec="seconds"), trip["name"],
        trip["origin"], trip["dest"], trip["depart"], trip["return"],
        adults, total, total / adults, solo, gap, r["n_options"],
        airline, itin.get("total_label"), daylight,
        json.dumps({"tier": r["tier"], "note": r.get("note")}),
        itin.get("stops"), itin.get("total_minutes"),
        1 if r["estimate"] else 0, r.get("gateway"), arrive_at,
        json.dumps(itin), r["tier"], r.get("cheapest_any"),
    ))
    conn.commit()

    print(f"  best that fits your rules ({adults} pax): ${total:,.0f}  "
          f"(${total/adults:,.0f} pp)"
          + ("   [ESTIMATE - summed legs, not bookable as one fare]"
             if r["estimate"] else ""))
    if r.get("cheapest_any") and r["cheapest_any"] < total:
        print(f"  cheapest on the board: ${r['cheapest_any']:,.0f} "
              f"-- breaks your rules, not tracked")
    line = summarize(itin)
    if line:
        print(f"  itinerary:  {line}")
    segs = itin.get("segments") or []
    lay = {l["at"]: l for l in itin.get("layovers") or []}
    for i, seg in enumerate(segs):
        dep = seg["dep"] and datetime.fromisoformat(seg["dep"])
        arr = seg["arr"] and datetime.fromisoformat(seg["arr"])
        bits = []
        if dep:
            bits.append(f"dep {dep:%a %d %b %H:%M}")
        if arr:
            bits.append(f"arr {arr:%H:%M}")
        if seg.get("minutes"):
            bits.append(_dur(seg["minutes"]))
        if seg.get("plane"):
            bits.append(seg["plane"])
        print(f"      {seg['from']}→{seg['to']:<4} "
              + ("  ".join(bits) if bits else ""))
        l = lay.get(seg["to"]) if i < len(segs) - 1 else None
        if l:
            print(f"        layover {l['at']}  {l.get('label') or '—'}")
    if r.get("note"):
        print(f"  note: {r['note']}")
    print(f"  options found: {r['n_options']}"
          + ("  (structural, not a scarcity signal on an estimate)"
             if r["estimate"] else ""))
    if arrive_at:
        print(f"  arrives {trip['dest']}: {arrive_at.replace('T', ' ')}"
              + ("  (daylight OK)" if daylight else "  (NOT a daylight arrival)"))
    if gap is not None:
        flag = "  <-- BUCKET THIN" if gap > BUCKET_GAP_ALERT_PCT else ""
        print(f"  bucket test: 1-pax ${solo:,.0f} x{adults} = ${solo*adults:,.0f} "
              f"vs ${total:,.0f}  -> gap {gap:+.1f}%{flag}")
    return total


def stats(conn, trip):
    """Return a JSON-serialisable snapshot of everything we know about a trip."""
    rows = conn.execute("""
        SELECT observed_at, total_price, bucket_gap, n_options, adults,
               best_airline, estimate, stops, arrive_at, itinerary, gateway,
               daylight_ok, duration_min, source, cheapest_any
        FROM observations WHERE trip=? AND total_price IS NOT NULL
        ORDER BY observed_at
    """, (trip["name"],)).fetchall()

    deadline = datetime.strptime(trip["deadline"], "%Y-%m-%d").date()
    base = {
        "trip": trip["name"],
        "route": f'{trip["origin"]}-{trip["dest"]}',
        "origin": trip["origin"],
        "dest": trip["dest"],
        "depart": trip["depart"],
        "ret": trip["return"],
        "adults": trip["adults"],
        "target": trip["target_price"],
        "deadline": trip["deadline"],
        "days_left": (deadline - date.today()).days,
        "n": len(rows),
        "series": [],
        "verdict": "NO DATA",
        "reasons": ["Run a refresh to collect the first observation."],
    }
    if not rows:
        return base

    prices = [r[1] for r in rows]
    latest = prices[-1]
    first_day = datetime.fromisoformat(rows[0][0]).date()
    last = rows[-1]
    last_gap, last_opts = last[2], last[3]
    is_estimate = bool(last[6])

    try:
        itin = json.loads(last[9]) if last[9] else None
    except (TypeError, ValueError):
        itin = None

    base.update({
        "current": latest,
        "per_person": latest / trip["adults"],
        "low": min(prices),
        "high": max(prices),
        "median": statistics.median(prices),
        "percentile": 100.0 * sum(1 for p in prices if p < latest) / len(prices),
        "vs_low_pct": (latest - min(prices)) / min(prices) * 100.0,
        "days_tracked": (date.today() - first_day).days,
        "bucket_gap": last_gap,
        "bucket_thin": bool(last_gap is not None and last_gap > BUCKET_GAP_ALERT_PCT),
        "n_options": last_opts,
        "airline": last[5],
        "observed_at": last[0],
        "estimate": is_estimate,
        "gateway": last[10],
        "stops": last[7],
        "arrive_at": last[8],
        "daylight_ok": bool(last[11]),
        "duration_min": last[12],
        "itinerary": itin,
        "itinerary_line": summarize(itin),
        "source": last[13],
        "cheapest_any": last[14],
        "series": [{"t": r[0], "p": r[1]} for r in rows],
    })

    for window in (7, 30):
        cutoff = datetime.now() - timedelta(days=window)
        past = [r[1] for r in rows if datetime.fromisoformat(r[0]) < cutoff]
        base[f"change_{window}d"] = (
            (latest - past[-1]) / past[-1] * 100.0 if past else None
        )

    reasons = []
    if latest <= trip["target_price"]:
        reasons.append(f"At or below your ${trip['target_price']:,} target")
    if base["bucket_thin"]:
        reasons.append(f"Cheap fare bucket is thin ({last_gap:+.1f}% gap)")
    # Only a real scarcity signal on a through-fare. On an estimated reading the
    # count is structural -- we deliberately query one nonstop per leg, so it is
    # always 1 and would otherwise fire BUY on the very first observation.
    if not is_estimate and last_opts is not None and last_opts <= 3:
        reasons.append(f"Only {last_opts} viable itineraries left")
    if base["days_left"] <= 0:
        reasons.append("Past your decide-by date")

    if reasons:
        base["verdict"], base["reasons"] = "BUY", reasons
    else:
        hold = [f"${latest:,.0f} is above your ${trip['target_price']:,} target",
                f"{base['days_left']} days until {trip['deadline']}"]
        if len(rows) < 10:
            hold.append(f"Only {len(rows)} observations - needs ~3 weeks to mean much")
        base["verdict"], base["reasons"] = "HOLD", hold

    if is_estimate:
        base["reasons"].append(
            f"Estimated: MCI→{base['gateway']}→{trip['dest']} priced separately "
            f"and summed — no through-fare is filed yet")
    return base


def report(conn, trip):
    s = stats(conn, trip)
    print(f"\n{'='*62}\n{s['trip']} - history\n{'='*62}")
    if not s["n"]:
        print("no data yet. run without --report first.")
        return
    print(f"observations: {s['n']} over {s['days_tracked']} day(s)")
    print(f"current:      ${s['current']:,.0f}"
          + ("  [estimate]" if s.get("estimate") else ""))
    if s.get("itinerary_line"):
        print(f"itinerary:    {s['itinerary_line']}")
    print(f"all-time low: ${s['low']:,.0f}      high: ${s['high']:,.0f}"
          f"      median: ${s['median']:,.0f}")
    print(f"today sits at the {s['percentile']:.0f}th percentile")
    print(f"vs low:       {s['vs_low_pct']:+.1f}%")
    for w in (7, 30):
        if s.get(f"change_{w}d") is not None:
            print(f"{w:>2}-day change: {s[f'change_{w}d']:+.1f}%")
    print(f"\n--- VERDICT ---\n{s['verdict']}  -> " + "; ".join(s["reasons"]))


def chart(conn):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("pip install matplotlib for charts")
        return
    fig, ax = plt.subplots(figsize=(10, 5))
    plotted = False
    for trip in TRIPS:
        rows = conn.execute("""
            SELECT observed_at, total_price FROM observations
            WHERE trip=? AND total_price IS NOT NULL ORDER BY observed_at
        """, (trip["name"],)).fetchall()
        if len(rows) < 2:
            continue
        xs = [datetime.fromisoformat(r[0]) for r in rows]
        ys = [r[1] for r in rows]
        ax.plot(xs, ys, marker="o", ms=3, label=trip["name"])
        ax.axhline(trip["target_price"], ls="--", lw=1, alpha=.5)
        plotted = True
    if not plotted:
        print("need at least 2 observations to chart")
        return
    ax.set_ylabel("total fare (USD)")
    ax.set_title("Fare history (dashed = your buy trigger)")
    ax.legend()
    ax.grid(alpha=.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig("pricehistory.png", dpi=130)
    print("wrote pricehistory.png")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="report only, no fetch")
    ap.add_argument("--chart", action="store_true", help="write pricehistory.png")
    ap.add_argument("--notify", action="store_true",
                    help="email if the fare moved (see notify.py)")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    active = [t for t in TRIPS if t.get("enabled")]
    if not args.report:
        for trip in active:
            record(conn, trip)
    for trip in active:
        report(conn, trip)
    if args.chart:
        chart(conn)

    if args.notify:
        import notify
        notify.run(conn, active)

    conn.close()


if __name__ == "__main__":
    main()
