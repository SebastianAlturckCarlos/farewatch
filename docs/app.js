/* Farewatch front end.
   Offline-first: paint from cache immediately, then update from the network.
   Never blocks on a request -- the point is that it answers in one glance. */

const app = document.getElementById("app");
const btn = document.getElementById("refresh");
const statusEl = document.getElementById("status");
const CACHE_KEY = "farewatch:last";

const money = n => "$" + Math.round(n).toLocaleString("en-US");
// `|| 0` collapses JavaScript's negative zero, which otherwise renders a
// bucket gap of -0.04% as a faintly alarming "-0.0%".
const pct = n => { const v = (Math.round(n * 10) / 10) || 0;
                   return (v >= 0 ? "+" : "") + v.toFixed(1) + "%"; };

function ago(iso) {
  const mins = Math.floor((Date.now() - new Date(iso)) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return mins + "m ago";
  const h = Math.floor(mins / 60);
  if (h < 24) return h + "h ago";
  return Math.floor(h / 24) + "d ago";
}

function dateline(t) {
  const f = s => new Date(s + "T00:00:00").toLocaleDateString("en-US",
    { day: "numeric", month: "short" }).toUpperCase();
  const nights = Math.round(
    (new Date(t.ret) - new Date(t.depart)) / 86400000);
  return `${f(t.depart)} · ${nights} NIGHTS · ${t.adults} PAX`;
}

/* ---- sparkline ---------------------------------------------------------- */
function drawSpark(svg, t) {
  const pts = t.series || [];
  svg.innerHTML = "";
  if (pts.length < 2) return;

  const W = 320, H = 72, pad = 6;
  const vals = pts.map(p => p.p);
  const lo = Math.min(...vals, t.target);
  const hi = Math.max(...vals, t.target);
  const span = hi - lo || 1;
  const x = i => (i / (pts.length - 1)) * W;
  const y = v => pad + (1 - (v - lo) / span) * (H - pad * 2);

  const ns = "http://www.w3.org/2000/svg";
  const make = (tag, attrs) => {
    const el = document.createElementNS(ns, tag);
    for (const k in attrs) el.setAttribute(k, attrs[k]);
    return el;
  };

  // shaded band below the buy trigger -- the zone you're waiting for
  const ty = y(t.target);
  if (ty < H) {
    svg.appendChild(make("rect",
      { class: "band", x: 0, y: ty, width: W, height: H - ty }));
  }
  svg.appendChild(make("line",
    { class: "trigger", x1: 0, y1: ty, x2: W, y2: ty }));

  const d = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.p).toFixed(1)}`).join(" ");
  svg.appendChild(make("path", { class: "line", d }));
  svg.appendChild(make("circle",
    { class: "head", cx: x(pts.length - 1), cy: y(vals[vals.length - 1]), r: 3 }));
}

/* ---- position bar ------------------------------------------------------- */
function drawPosition(el, t) {
  const lo = Math.min(t.low, t.target);
  const hi = Math.max(t.high, t.target);
  const span = hi - lo || 1;
  const at = v => Math.max(0, Math.min(100, ((v - lo) / span) * 100));

  el.fill.style.width = at(t.current) + "%";
  el.marker.style.left = at(t.current) + "%";
  el.notch.style.left = at(t.target) + "%";
  el.low.textContent = money(lo);
  el.high.textContent = money(hi);
}

/* ---- the board ----------------------------------------------------------
   Showing one itinerary made every other number unverifiable -- you could see
   "$2,179, BUY" but not why, or what it was chosen over. The whole board goes
   on screen, cheapest first, with the ones that break your rules dimmed and
   labelled with what disqualifies them. On this route that reads at a glance:
   everything cheap lands the next day. */
const T = iso => (iso ? iso.slice(11, 16) : "—");

function flightRow(b) {
  const nextDay = b.depart_at && b.arrive_at
    && b.arrive_at.slice(0, 10) !== b.depart_at.slice(0, 10);

  const via = b.stops === 0
    ? "nonstop"
    : `${b.stops} stop${b.stops > 1 ? "s" : ""} ${(b.via || []).join(", ")}`;
  // A layover's length is the difference between a tolerable connection and a
  // day at an airport, so it earns the space. With two stops, the longer one
  // is the one that decides how the day feels.
  const mins = (b.layovers || []).map(l => l.minutes).filter(Boolean);
  const worst = mins.length
    ? (b.layovers.find(l => l.minutes === Math.max(...mins)) || {}).label
    : null;
  const lay = worst ? ` (${worst})` : "";

  const detail = [via + lay, b.total_label, b.airline].filter(Boolean).join(" · ");

  return `<li class="flight${b.fits ? "" : " off"}${b.tracked ? " tracked" : ""}">
      ${b.tracked ? '<span class="tag">tracking</span>' : ""}
      <span class="price">$${b.price.toLocaleString("en-US")}</span>
      <span class="when">${T(b.depart_at)} → ${T(b.arrive_at)}${
        nextDay ? '<i class="plus">+1</i>' : ""}</span>
      <span class="detail">${detail}</span>
      ${b.why ? `<span class="why">${b.why}</span>` : ""}
      ${b.tight && !b.why ? '<span class="why tight">tight connection</span>' : ""}
    </li>`;
}

function renderBoard(f, t) {
  const rows = t.board || [];
  if (!rows.length) { f.boardbox.hidden = true; return; }
  f.boardbox.hidden = false;

  const fits = rows.filter(b => b.fits).length;
  f.boardcount.textContent = `${rows.length} shown · ${fits} fit your rules`;
  f.flights.innerHTML = rows.map(flightRow).join("");

  f.boardnote.classList.remove("warn");
  if (t.estimate) {
    f.boardnote.textContent =
      "Estimated — priced as two separate tickets, not one bookable fare.";
    f.boardnote.classList.add("warn");
  } else if (fits === 0) {
    f.boardnote.textContent =
      "Nothing on the board meets your rules right now.";
    f.boardnote.classList.add("warn");
  } else {
    f.boardnote.textContent =
      "Dimmed flights break your rules: more stops than you want, a layover "
      + "over 4 hours, or a next-day landing that costs a night of the trip.";
  }
}

/* ---- render ------------------------------------------------------------- */
function renderTrip(t) {
  const node = document.getElementById("tpl-trip").content.cloneNode(true);
  const f = {};
  node.querySelectorAll("[data-f]").forEach(n => f[n.dataset.f] = n);

  f.origin.textContent = t.origin;
  f.dest.textContent = t.dest;
  f.dateline.textContent = dateline(t);

  const section = node.querySelector(".trip");
  section.style.setProperty("--state",
    t.verdict === "BUY" ? "var(--signal)" : "var(--calm)");

  f.verdict.textContent = t.verdict;

  if (!t.n) {
    f.fare.textContent = "—";
    f.faresub.textContent = "No observations yet";
    f.asof.textContent = "never";
    f.dot.classList.add("stale");
    f.reasons.innerHTML = "<li>Tap “Check fares now” to take the first reading.</li>";
    return node;
  }

  f.fare.textContent = money(t.current);
  f.faresub.textContent =
    `${money(t.per_person)} per person · ${t.airline || "—"}`;

  const stale = (Date.now() - new Date(t.observed_at)) > 36 * 3600 * 1000;
  f.dot.classList.toggle("stale", stale);
  f.asof.textContent = ago(t.observed_at);

  drawPosition(f, t);
  drawSpark(f.spark, t);
  f.sparklbl.textContent =
    `${t.n} readings · ${t.days_tracked} days · low ${money(t.low)}`;

  f.n.textContent = t.n;
  f.pctl.textContent = Math.round(t.percentile) + "th";
  f.d7.textContent = t.change_7d == null ? "—" : pct(t.change_7d);

  if (t.bucket_gap == null) {
    f.bucket.textContent = "—";
  } else if (t.bucket_thin) {
    f.bucket.textContent = `Thin ${pct(t.bucket_gap)}`;
    f.bucket.classList.add("warn");
  } else {
    f.bucket.textContent = `Healthy ${pct(t.bucket_gap)}`;
  }

  // On an estimated reading the count is structural -- we query exactly one
  // nonstop per leg, so it is always 1 and means nothing about scarcity.
  if (t.estimate) {
    f.opts.textContent = "n/a · estimated";
  } else {
    f.opts.textContent = t.n_options ?? "—";
    if (t.n_options != null && t.n_options <= 3) f.opts.classList.add("warn");
  }

  renderBoard(f, t);
  f.deadline.textContent = `${t.deadline} · ${t.days_left}d`;
  f.reasons.innerHTML = t.reasons.map(r => `<li>${r}</li>`).join("");
  return node;
}

function render(data) {
  app.innerHTML = "";
  data.trips.forEach(t => app.appendChild(renderTrip(t)));
}

/* ---- data --------------------------------------------------------------- */
function cached() {
  try { return JSON.parse(localStorage.getItem(CACHE_KEY)); }
  catch { return null; }
}

function store(data) {
  try { localStorage.setItem(CACHE_KEY, JSON.stringify(data)); } catch {}
}

async function load(announce) {
  const c = cached();
  if (c) render(c);

  try {
    const res = await fetch("data.json?t=" + Date.now(), { cache: "no-store" });
    if (!res.ok) throw new Error(res.status);
    const data = await res.json();
    render(data);
    store(data);
    statusEl.textContent = data.warning
      ? data.warning
      : (announce ? "Up to date · collected " + ago(data.generated_at) : "");
  } catch {
    statusEl.textContent = c
      ? "Offline — showing last saved reading"
      : "Couldn't load data.json. Has the workflow run yet?";
    if (!c) app.innerHTML =
      '<div class="boot">No readings yet. Run the Track fares workflow ' +
      'from the Actions tab, then pull down to refresh.</div>';
  }
}

// Readings are taken by the scheduled job, not the phone. The button pulls
// the newest committed reading rather than pretending to scrape from here.
btn.addEventListener("click", async () => {
  btn.disabled = true;
  btn.textContent = "Loading…";
  await load(true);
  btn.disabled = false;
  btn.textContent = "Reload latest";
});

load();
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) load();
});

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("sw.js").catch(() => {});
}
