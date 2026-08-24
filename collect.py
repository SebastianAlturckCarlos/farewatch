#!/usr/bin/env python3
"""
collect.py - the GitHub Actions entry point.

Loads the committed JSON history, takes one live reading, recomputes stats, and
writes docs/data.json for the static site to consume. History lives in JSON
rather than SQLite so git diffs stay readable.

    python collect.py
"""

import json
import os
import sqlite3
import sys
from datetime import datetime

import farewatch as fw

DOCS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
HISTORY = os.path.join(DOCS, "history.json")
DATA = os.path.join(DOCS, "data.json")
# What we last emailed about. Committed with the history so the scheduled
# job remembers across runs and does not re-announce the same fare.
NOTIFIED = os.path.join(DOCS, "notified.json")

COLUMNS = ["observed_at", "trip", "origin", "dest", "depart", "ret", "adults",
           "total_price", "per_person", "solo_price", "bucket_gap", "n_options",
           "best_airline", "best_duration", "daylight_ok", "raw"]


def load_into_memory():
    """Rehydrate JSON history into an in-memory DB so we can reuse fw.stats()."""
    conn = sqlite3.connect(":memory:")
    fw.init_db(conn)
    if os.path.exists(HISTORY):
        with open(HISTORY) as fh:
            rows = json.load(fh)
        for r in rows:
            conn.execute(
                f"INSERT INTO observations ({','.join(COLUMNS)}) "
                f"VALUES ({','.join('?' * len(COLUMNS))})",
                [r.get(c) for c in COLUMNS])
        conn.commit()
    return conn


def dump_history(conn):
    rows = conn.execute(
        f"SELECT {','.join(COLUMNS)} FROM observations ORDER BY observed_at"
    ).fetchall()
    with open(HISTORY, "w") as fh:
        json.dump([dict(zip(COLUMNS, r)) for r in rows], fh, indent=1)
    return len(rows)


def main():
    os.makedirs(DOCS, exist_ok=True)
    conn = load_into_memory()
    active = [t for t in fw.TRIPS if t.get("enabled")]

    failures = []
    for trip in active:
        try:
            if fw.record(conn, trip) is None:
                failures.append(trip["name"])
        except Exception as e:
            print(f"  ! {trip['name']}: {type(e).__name__}: {e}")
            failures.append(trip["name"])

    n = dump_history(conn)
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "trips": [fw.stats(conn, t) for t in active],
    }
    if failures:
        payload["warning"] = "No fares returned for: " + ", ".join(failures)
    with open(DATA, "w") as fh:
        json.dump(payload, fh, indent=1)

    # Mail only if something actually moved. Never fail the run on a mail
    # problem -- a missed email is better than a red X and a lost reading.
    try:
        import notify
        cfg = notify.load_env()
        if notify.missing(cfg):
            print("  [notify] SMTP not configured; skipping email")
        else:
            notify.run(conn, active, cfg, store=notify.JsonStore(NOTIFIED))
    except Exception as e:
        print(f"  [notify] skipped: {type(e).__name__}: {e}")

    conn.close()

    print(f"\nwrote {DATA} ({n} total observations)")
    # Don't fail the workflow on a single bad scrape -- a gap in the series is
    # better than a red X every time Google hiccups. Only fail if nothing ever
    # worked, which means the setup itself is broken.
    if failures and n == 0:
        sys.exit("no data collected at all -- check the scraper")


if __name__ == "__main__":
    main()
