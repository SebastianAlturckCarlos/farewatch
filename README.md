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

Two sources, tried in order.

**`gflights.py` — headless Chromium, primary.** For MCI→UVF, Google returns a
1.8 MB page containing *zero* prices: the results are rendered client-side by
JavaScript. Nothing that fetches HTML can read that — not `fast-flights`, not a
different library, not a hand-rolled request. Confirmed by fetching the page
directly and counting: 0 price strings on MCI→UVF, 42 on MIA→UVF. The page has
to execute JS, so the collector drives a real browser.

It reads each result's `aria-label`, which is a complete sentence:

> From 2179 US dollars round trip total. 1 stop flight with American. Leaves
> Kansas City International Airport at 5:00 AM on Monday, June 14 and arrives
> at Hewanorra International Airport at 1:58 PM on Monday, June 14. Total
> duration 7 hr 58 min. Layover (1 of 1) is a 55 min layover at Miami…

That is a far more durable contract than Google's obfuscated CSS class names,
which change without notice. Accessibility labels don't, because screen readers
depend on them.

**`fast-flights` — fallback.** Still the right tool for routes Google
server-renders, and much faster (~2s against ~15s). Pin the version; when a
refresh starts failing, `pip install -U fast-flights` is usually the fix. Note
that 3.1.0 imports `typing_extensions` without declaring it as a dependency, so
a clean environment needs it installed explicitly.

The **Amadeus Self-Service API** used to be the obvious upgrade. That door is
closed: Amadeus decommissioned the portal on **17 July 2026** and deactivated
existing keys. There is no free replacement with real GDS data — Duffel, Kiwi,
Ignav and SerpApi are paid, gated behind a business account, or both.

### Cheapest is not what gets tracked

The fare recorded is the cheapest itinerary that **meets your rules** —
`max_stops`, `same_day_arrival`, and `latest_arrival_hour`. Right now that is
$2,179: one stop at MIA, 05:00 → 13:58 the same day, 7h 58m.

The board's cheapest is $1,587, and the app says so underneath. It is $592 less
because it takes two stops and lands at 14:10 **the next day** — which costs a
night of a five-night trip and misses the daylight transfer to La Toc. That is
a trade worth seeing, not a number worth tracking.

Because of this, **"viable itineraries" is now a real scarcity signal**: it
counts the options that actually satisfy your constraints. When it falls to two
or three, seats are going regardless of what the headline fare does.

### When the browser has a bad run

Roughly one run in four, the board doesn't finish rendering inside the timeout,
so `gflights` retries three times. If all three fail, the reading is **skipped**
rather than falling back to the leg-summed estimate. An estimate is not a
degraded reading of the same quantity — it prices two separate tickets, nonstop
legs only, and lands about $500 high. Dropping that into a series of real fares
would corrupt the low, the high, the percentile, and every alert that reads
them. A gap in the series is honest; a wrong number is not.

## Known limits

Nothing here predicts fares. Airlines reprice on models you can't observe. What
this gives you is a **record instead of an impression** — so the March 1
decision gets made against six months of data rather than the memory of two bad
pings.

Serving over plain HTTP on your LAN means no HTTPS, and some mobile browsers
refuse service workers on bare LAN IPs. The GitHub Pages setup in `SETUP.md`
is real HTTPS and does not have this problem.
