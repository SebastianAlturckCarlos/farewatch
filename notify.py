#!/usr/bin/env python3
"""
notify.py - email you when the fare MOVES, and stay silent otherwise.

The whole point is that a quiet inbox means "nothing changed". If this thing
mails you every morning you will stop reading it by week two, and then it is
worse than useless -- it is noise that hides the one message that mattered.

So it compares against the last reading it actually TOLD you about, not the
last reading it took. Six days of $2,711 followed by $2,690 is a $21 drift, not
news, and you hear nothing. The day it steps to $2,400 you get one email.

Fires on any of:
  * price moved >= $40 or >= 2% since the last email
  * the verdict flipped (HOLD <-> BUY)
  * the cheap fare bucket just went thin
  * a new all-time low
  * the itinerary got materially worse (extra stop, or lost daylight arrival)

Config -- environment variables, or a file called `farewatch.env` sitting next
to this script with KEY=VALUE lines:

    FAREWATCH_TO=sebastianrhoton@gmail.com
    FAREWATCH_SMTP_USER=sebastianrhoton@gmail.com
    FAREWATCH_SMTP_PASS=<16-char Gmail app password, no spaces>
    FAREWATCH_SMTP_HOST=smtp.gmail.com     # optional, this is the default
    FAREWATCH_SMTP_PORT=587                # optional, this is the default

Gmail will not accept your normal password. Create an app password at
https://myaccount.google.com/apppasswords (requires 2-step verification on).

    python3 notify.py --test     # send one mail right now, prove it works
    python3 notify.py            # evaluate and send only if something moved
"""

import json
import os
import smtplib
import sqlite3
import sys
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate

import farewatch as fw

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(HERE, "farewatch.env")

# What counts as a move. Either threshold alone is enough.
MOVE_ABS = 40.0
MOVE_PCT = 2.0


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def load_env():
    """Environment wins; farewatch.env fills the gaps."""
    cfg = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("FAREWATCH_TO", "FAREWATCH_SMTP_USER", "FAREWATCH_SMTP_PASS",
              "FAREWATCH_SMTP_HOST", "FAREWATCH_SMTP_PORT"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    cfg.setdefault("FAREWATCH_SMTP_HOST", "smtp.gmail.com")
    cfg.setdefault("FAREWATCH_SMTP_PORT", "587")
    return cfg


def missing(cfg):
    return [k for k in ("FAREWATCH_TO", "FAREWATCH_SMTP_USER",
                        "FAREWATCH_SMTP_PASS") if not cfg.get(k)]


# ---------------------------------------------------------------------------
# state: what did we last tell them?
# ---------------------------------------------------------------------------

FIELDS = ("sent_at", "price", "verdict", "bucket_thin", "low", "stops",
          "daylight_ok")


def snapshot(s):
    """The bits of a reading we compare against next time."""
    return {
        "sent_at": datetime.now().isoformat(timespec="seconds"),
        "price": s["current"],
        "verdict": s["verdict"],
        "bucket_thin": bool(s.get("bucket_thin")),
        "low": s.get("low"),
        "stops": s.get("stops"),
        "daylight_ok": bool(s.get("daylight_ok")),
    }


class SqliteStore:
    """Local runs: state lives beside the observations, in fares.db."""

    def __init__(self, conn):
        self.conn = conn
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                trip         TEXT PRIMARY KEY,
                sent_at      TEXT,
                price        REAL,
                verdict      TEXT,
                bucket_thin  INTEGER,
                low          REAL,
                stops        INTEGER,
                daylight_ok  INTEGER
            )
        """)
        conn.commit()

    def get(self, trip):
        row = self.conn.execute(
            f"SELECT {','.join(FIELDS)} FROM notifications WHERE trip=?",
            (trip,)).fetchone()
        return dict(zip(FIELDS, row)) if row else None

    def put(self, trip, snap):
        self.conn.execute(
            f"INSERT INTO notifications (trip,{','.join(FIELDS)}) "
            f"VALUES ({','.join('?' * (len(FIELDS) + 1))}) "
            f"ON CONFLICT(trip) DO UPDATE SET "
            + ", ".join(f"{f}=excluded.{f}" for f in FIELDS),
            (trip, *(snap[f] for f in FIELDS)))
        self.conn.commit()


class JsonStore:
    """CI runs: fares.db is rebuilt in memory each time, so state has to be a
    file the workflow can commit back to the repo alongside the history."""

    def __init__(self, path):
        self.path = path
        try:
            with open(path, encoding="utf-8") as fh:
                self.data = json.load(fh)
        except (OSError, ValueError):
            self.data = {}

    def get(self, trip):
        return self.data.get(trip)

    def put(self, trip, snap):
        self.data[trip] = snap
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=1, sort_keys=True)


# ---------------------------------------------------------------------------
# did anything actually move?
# ---------------------------------------------------------------------------

def changes(s, prev):
    """Return a list of human-readable reasons to mail, empty if none."""
    if not s.get("n"):
        return []

    now = s["current"]
    if prev is None:
        return [f"First reading: {_m(now)}"]

    out = []
    was = prev["price"]
    if was:
        delta = now - was
        pct = delta / was * 100.0
        if _material(abs(delta), was):
            arrow = "fell" if delta < 0 else "rose"
            out.append(f"Fare {arrow} {_m(abs(delta))} ({pct:+.1f}%) — "
                       f"{_m(was)} → {_m(now)}")

    if s["verdict"] != prev.get("verdict"):
        out.append(f"Verdict flipped {prev.get('verdict')} → {s['verdict']}")

    if bool(s.get("bucket_thin")) and not prev.get("bucket_thin"):
        out.append(f"Cheap fare bucket just went thin "
                   f"({s['bucket_gap']:+.1f}% gap) — seats are going")

    if (prev.get("stops") is not None and s.get("stops") is not None
            and s["stops"] > prev["stops"]):
        out.append(f"Routing got worse: {prev['stops']} → {s['stops']} stops")

    if prev.get("daylight_ok") and not s.get("daylight_ok"):
        out.append("Lost the daylight arrival — no more same-day landing "
                   "before your cutoff")

    # A new all-time low is worth saying, but on its own it is not worth an
    # email: on a series that only ever drifts downward every single reading
    # is technically a new low, which is how a useful alert turns into daily
    # spam. It has to clear the same bar as any other move, or ride along with
    # something that already did.
    prev_low = prev.get("low")
    if prev_low is not None and s["low"] < prev_low:
        if out or _material(prev_low - s["low"], prev_low):
            out.append(f"New all-time low: {_m(s['low'])}")

    return out


def _material(delta, base):
    """Is a change big enough to be worth interrupting someone over?"""
    return delta >= MOVE_ABS or (base and delta / base * 100.0 >= MOVE_PCT)


def _m(n):
    return "$" + format(round(n), ",")


# ---------------------------------------------------------------------------
# the email
# ---------------------------------------------------------------------------

def compose(s, reasons):
    headline = reasons[0]
    subject = f"{s['trip']} {_m(s['current'])} — {headline.split('—')[0].strip()}"
    if s["verdict"] == "BUY":
        subject = f"BUY · {subject}"

    lines = [
        f"{s['trip']}   {s['origin']} → {s['dest']}",
        f"{s['depart']} to {s['ret']} · {s['adults']} passengers",
        "",
        f"{_m(s['current'])} total   ({_m(s['per_person'])} per person)"
        + ("   [ESTIMATE — legs priced separately]" if s.get("estimate") else ""),
        "",
        "WHAT MOVED",
    ]
    lines += [f"  · {r}" for r in reasons]

    if s.get("itinerary_line"):
        lines += ["", "ITINERARY", f"  {s['itinerary_line']}"]
        for seg in (s.get("itinerary") or {}).get("segments") or []:
            dep = seg["dep"] and datetime.fromisoformat(seg["dep"])
            arr = seg["arr"] and datetime.fromisoformat(seg["arr"])
            when = f"{dep:%a %d %b  %H:%M} → {arr:%H:%M}" if dep and arr else "—"
            lines.append(f"    {seg['from']}→{seg['to']}  {when}"
                         f"  {seg.get('plane') or ''}")
        for lay in (s.get("itinerary") or {}).get("layovers") or []:
            lines.append(f"    layover {lay['at']}: {lay.get('label') or '—'}")

    lines += [
        "",
        "WHERE THIS SITS",
        f"  Target      {_m(s['target'])}",
        f"  Range seen  {_m(s['low'])} – {_m(s['high'])}   "
        f"(today: {round(s['percentile'])}th percentile)",
        f"  History     {s['n']} readings over {s['days_tracked']} days",
        f"  Decide by   {s['deadline']}  ({s['days_left']} days left)",
        "",
        f"VERDICT: {s['verdict']}",
    ]
    lines += [f"  · {r}" for r in s["reasons"]]
    lines += ["", "— farewatch. You only hear from it when something moves."]

    body = "\n".join(lines)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)
    return msg


def send(cfg, msg):
    msg["From"] = cfg["FAREWATCH_SMTP_USER"]
    msg["To"] = cfg["FAREWATCH_TO"]
    host, port = cfg["FAREWATCH_SMTP_HOST"], int(cfg["FAREWATCH_SMTP_PORT"])
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls()
    with server:
        server.login(cfg["FAREWATCH_SMTP_USER"], cfg["FAREWATCH_SMTP_PASS"])
        server.send_message(msg)


# ---------------------------------------------------------------------------

def run(conn, trips, cfg=None, force=False, store=None):
    """Evaluate every active trip and mail only the ones that moved."""
    cfg = cfg or load_env()
    gaps = missing(cfg)
    if gaps:
        print(f"  [notify] not configured — missing {', '.join(gaps)}. "
              f"See {ENV_FILE}")
        return 0

    store = store or SqliteStore(conn)
    sent = 0
    for trip in trips:
        s = fw.stats(conn, trip)
        prev = store.get(trip["name"])
        reasons = changes(s, prev) if not force else ["Test message — forced send"]
        if not reasons:
            print(f"  [notify] {trip['name']}: no movement, staying quiet")
            continue
        try:
            send(cfg, compose(s, reasons))
        except Exception as e:
            print(f"  [notify] {trip['name']}: send FAILED — {type(e).__name__}: {e}")
            continue
        print(f"  [notify] {trip['name']}: emailed {cfg['FAREWATCH_TO']} — "
              + "; ".join(reasons))
        if not force:
            store.put(trip["name"], snapshot(s))
        sent += 1
    return sent


def main():
    force = "--test" in sys.argv
    conn = sqlite3.connect(fw.DB_PATH)
    fw.init_db(conn)
    active = [t for t in fw.TRIPS if t.get("enabled")]
    cfg = load_env()
    gaps = missing(cfg)
    if gaps:
        print("Email is not configured yet.\n")
        print(f"Create {ENV_FILE} with:\n")
        print("  FAREWATCH_TO=sebastianrhoton@gmail.com")
        print("  FAREWATCH_SMTP_USER=sebastianrhoton@gmail.com")
        print("  FAREWATCH_SMTP_PASS=<gmail app password>\n")
        print("App password: https://myaccount.google.com/apppasswords")
        sys.exit(1)
    n = run(conn, active, cfg, force=force)
    conn.close()
    print(f"\n{n} email(s) sent.")


if __name__ == "__main__":
    main()
