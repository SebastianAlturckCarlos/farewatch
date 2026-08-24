/* Farewatch front end.
   Offline-first: paint from cache immediately, then update from the network.
   Never blocks on a request -- the point is that it answers in one glance. */

const app = document.getElementById("app");
const btn = document.getElementById("refresh");
const statusEl = document.getElementById("status");
const CACHE_KEY = "farewatch:last";

const money = n => "$" + Math.round(n).toLocaleString("en-US");
const pct = n => (n >= 0 ? "+" : "") + n.toFixed(1) + "%";

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

/* ---- itinerary ----------------------------------------------------------
   The headline number is only half the story. A fare that drops $200 by
   adding a six-hour layover has not got cheaper, so the whole routing rides
   along with every reading and gets shown as a boarding-pass stub. */
const hhmm = iso => (iso ? iso.slice(11, 16) : "");
const dayOf = iso => (iso
  ? new Date(iso).toLocaleDateString("en-US",
      { weekday: "short", day: "numeric", month: "short" })
  : "");

function renderItinerary(f, t) {
  const it = t.itinerary;
  const segs = (it && it.segments) || [];
  if (!segs.length) { f.itinbox.hidden = true; return; }

  f.itinbox.hidden = false;
  f.itinline.textContent = t.itinerary_line || "";
  f.estbadge.hidden = !t.estimate;

  const lay = {};
  (it.layovers || []).forEach(l => { lay[l.at] = l; });

  f.segs.innerHTML = segs.map((s, i) => {
    const l = i < segs.length - 1 ? lay[s.to] : null;
    const layRow = l
      ? `<li class="lay"><span>${l.at} layover</span><span>${l.label || "—"}</span></li>`
      : "";
    return `<li class="seg">
        <span class="hop">${s.from}<i>→</i>${s.to}</span>
        <span class="when">${dayOf(s.dep)} · ${hhmm(s.dep)}–${hhmm(s.arr)}</span>
        <span class="plane">${s.plane || ""}</span>
      </li>${layRow}`;
  }).join("");

  const c = it.connection;
  f.itinnote.classList.remove("warn");
  if (c && c.ok === false) {
    f.itinnote.textContent = c.label;
    f.itinnote.classList.add("warn");
  } else if (t.estimate) {
    f.itinnote.textContent =
      "Legs priced separately — no through-fare is filed yet, so this is an estimate.";
  } else if (t.arrive_at && !t.daylight_ok) {
    f.itinnote.textContent = "Not a daylight arrival.";
    f.itinnote.classList.add("warn");
  } else {
    f.itinnote.textContent = "";
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

  renderItinerary(f, t);
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
