# Farewatch

Fare tracking for MCI → UVF (St Lucia, 14–19 Jun 2027) that answers one
question in one glance: **buy today, or hold?**

Runs as a phone app you install from your home screen, backed by a small Flask
server on your own machine.

---

## Install

```bash
pip install flask fast-flights matplotlib qrcode
python3 app.py
```

On startup it prints the exact URL for your phone, plus a QR code you can scan.
Same wifi, then:
**iPhone** Share → Add to Home Screen · **Android** menu → Install app

It installs standalone: no browser chrome, its own icon, opens instantly to the
last known fare even with no signal.

Keep a scheduled run going so history builds whether or not you open the app.

**Windows** — registers a task at 8:10am and 8:10pm, catches up after sleep:

```
powershell -ExecutionPolicy Bypass -File install-task.ps1
```

**macOS / Linux** — cron:

```
10 8,20 * * *  cd ~/farewatch && /usr/bin/python3 farewatch.py --notify >> farewatch.log 2>&1
```

The app *reads* history. Cron *builds* it. **Check fares now** forces a live
read when you want one.

---

## Reading the screen

**The verdict** is the only coloured thing on the page. Orange means buy, teal
means hold. Nothing else is tinted, so you can't misread it in a hurry.

**The position bar** is your percentile made physical — where today's fare sits
between the cheapest and dearest you've ever recorded, with a notch at your
$2,200 trigger. Marker left of the notch means you're in buy territory.

**The sparkline** shades the zone below your trigger. You're watching for the
line to enter the shaded band.

**Fare bucket** is the early warning. It prices 1 passenger and 2 passengers
separately: a 2-pax search needs both seats in the same bucket, so when
2×(1-pax) comes in well under the 2-pax fare, the cheap bucket is nearly empty.
Marked "Thin" past 15%. This moves *before* the price does.

**Viable itineraries** counts routings with ≤1 stop arriving before 18:00 —
daylight landing, since you've got a ~90 minute transfer to La Toc. When it
drops to 2–3, seats are going regardless of the headline fare.

---

## Who does what

Two things run on a schedule, and they do not overlap:

| | GitHub Actions | Windows task |
|---|---|---|
| Runs | 8am / 8pm Central, in the cloud | 8:10am / 8:10pm, on this PC |
| Records to | `docs/history.json` (committed) | `fares.db` (local) |
| Updates the phone app | yes | no |
| **Emails you** | **yes** | no, by default |
| Works with the PC off | yes | no |

Actions is the one that matters: it feeds the phone app and sends the alerts,
and it keeps its "already told you about this" state in `docs/notified.json`
so it never repeats itself. The Windows task is insurance — it keeps an
independent local series in case Google ever starts serving the cloud runners
a bot challenge instead of fares, which is the one failure this setup is
exposed to.

They deliberately do not both email. If you ever want to flip ownership to this
machine, delete the workflow and re-register the task with
`install-task.ps1 -WithEmail`.

## Email alerts

You hear from it when the fare **moves**, and not otherwise. A quiet inbox
means nothing changed — that is the whole design. It compares against the last
reading it actually emailed you about, not the last one it took, so a slow
$8-a-day drift never accumulates into a daily nag.

It mails you when any of these happen:

| Trigger | Threshold |
|---|---|
| Fare moved | ≥ $40 or ≥ 2% since the last email |
| Verdict flipped | HOLD ↔ BUY |
| Cheap fare bucket went thin | gap > 15% |
| New all-time low | must also clear the move threshold |
| Routing got worse | an extra stop, or the daylight arrival disappeared |

Setup — two minutes:

1. Turn on 2-Step Verification, then generate a 16-character app password at
   <https://myaccount.google.com/apppasswords>. Gmail rejects your normal
   password over SMTP.
2. Paste it into `farewatch.env` as `FAREWATCH_SMTP_PASS` (no spaces).
   That file is gitignored — the password never leaves this machine.
3. Prove it works: `python3 notify.py --test` sends one mail immediately.

`--test` does not update the "last emailed" state, so it can't make you miss a
real alert.

## CLI

The terminal version still works and shares the same database and logic:

```bash
python3 farewatch.py            # fetch, record, report
python3 farewatch.py --report   # report only, no network
python3 farewatch.py --chart    # writes pricehistory.png
```

## Configuration

Everything lives in the `TRIPS` block at the top of `farewatch.py`. The bachelor
trip (MCI → PUJ) is already stubbed in — flip `enabled` to `True` if you end up
booking those flights separately rather than as part of a package.

---

## Files

```
app.py               Flask server: JSON API + PWA hosting
farewatch.py         fetching, storage, stats, verdict logic
static/index.html    app shell
static/app.css       styling
static/app.js        rendering, offline-first data loading
static/sw.js         service worker (cache-first shell, network-first data)
static/manifest.json PWA manifest
fares.db             SQLite history (created on first run)
```

## Data source

`fast-flights` (3.1.0) scrapes Google Flights through its protobuf query
parameter. It is the only free source still standing, and it is a better one
than it looks: every reading comes back with the **whole itinerary** — each
segment, both airport codes, local departure and arrival stamps, leg duration
and aircraft type — not just a price. All of that is recorded and shown.

The obvious upgrade used to be the **Amadeus Self-Service API**. That door is
closed: Amadeus decommissioned the Self-Service portal on **17 July 2026** and
deactivated existing keys. There is no free replacement with real GDS data —
the remaining options (Duffel, Kiwi, Ignav, SerpApi) are paid, gated behind a
business account, or both. So: stay on `fast-flights`, pin the version, and
when a refresh starts failing try `pip install -U fast-flights` first.

### The MCI→UVF quirk

Google will not build MCI→UVF connecting itineraries for *any* 2027 date yet,
though it prices MCI→UVF fine for dates a couple of months out, and prices
CLT/MIA/ATL/JFK→UVF fine for June 2027. When there is no through-fare,
farewatch prices `MCI→gateway` and `gateway→UVF` separately and sums them.

Those readings are labelled **estimate**, in the app and in the emails, because
that is what they are — two fares that happen to add up, not one you can book.
It also checks whether the halves actually connect. Right now they do not: the
AA nonstop MIA→UVF pushes back at 10:10, and no MCI feeder lands before 15:36.
**There is no same-day MCI→UVF connection as currently filed** — the real trip
needs a gateway overnight or a day-earlier departure, and the estimate does not
include that hotel night. The app says so on the itinerary line.

This resolves itself: the day a through-fare gets filed, the estimate flag
disappears on its own.

## Known limits

Nothing here predicts fares. Airlines reprice on models you can't observe. What
this gives you is a **record instead of an impression** — so the March 1
decision gets made against six months of data rather than the memory of two bad
pings.

Serving over plain HTTP on your LAN means no HTTPS, and some mobile browsers
refuse service workers on bare LAN IPs. The GitHub Pages setup in `SETUP.md`
is real HTTPS and does not have this problem.
