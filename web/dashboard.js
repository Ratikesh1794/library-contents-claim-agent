// Laptop dashboard: every record organised, and everything happening in the sweep, live.
// Data: GET /api/sweeps (picker), /ws/live?role=dashboard (live events + history replay), /api/sweeps/<id>/packet,
// /api/sweeps/<id>/crop (record thumbnails). Server side: backend/live/app.py.
"use strict";
const $ = (id) => document.getElementById(id);
const TOKEN = new URLSearchParams(location.search).get("t") || "";
const q = (o) => new URLSearchParams({ ...o, t: TOKEN }).toString();
const api = (path) => fetch(path, { headers: { "X-Sweep-Token": TOKEN } });
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const clock = (t) => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`;
const money = (v, cur) => (v == null ? "—" : new Intl.NumberFormat(undefined, { style: "currency", currency: cur || "USD", maximumFractionDigits: 0 }).format(v));
const words = (s) => String(s || "").replace(/_/g, " ");
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
const crop = (r, w) => `/api/sweeps/${sweepId}/crop?${q({ f: r.frame, b: r.bbox.join(","), w })}`;

let sweepId = null, ws = null, follow = true, currency = "USD", phoneOnline = false, qrDismissed = false, sweeps = [];
let inv = { books: [], items: [], counts: {} }, review = [], tab = "books", filter = "all", seenIds = new Set();
let sort = { key: null, dir: 1 };

// ---------------------------------------------------------------- status chips
function pill(id, text, s) { const p = $(id); p.dataset.s = s; p.querySelector("span").textContent = text; }
const VOICE = { listening: ["Agent listening", "ok"], hearing: ["Hearing the user", "live"], thinking: ["Agent thinking", "warn"],
                speaking: ["Agent speaking", "ok"] };

// ---------------------------------------------------------------- sweep picker (+ follow the newest live sweep)
const when = (s) => (s.created_at ? new Date(s.created_at * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : s.id);

async function loadSweeps() {
  const r = await api("/api/sweeps"); if (!r.ok) { pill("pillConn", r.status === 401 ? "Link expired: reopen from the terminal" : "Server error", "bad"); return; }
  sweeps = await r.json();
  const pick = $("sweepPick"), current = pick.value;
  pick.replaceChildren(...sweeps.map((s) =>
    new Option(`${s.live ? "● Live · " : ""}${when(s)} · ${s.country || "?"} · ${plural(s.frames, "frame")}${s.packet ? " · packet" : ""}`, s.id)));
  if (!sweeps.length) { pick.append(new Option("No sweeps yet", "")); return; }
  const target = follow ? (sweeps.find((s) => s.live) || sweeps[0]).id : (current || sweeps[0].id);
  pick.value = target;
  if (target !== sweepId) openSweep(target, sweeps.find((s) => s.id === target));
  else hero();
}
$("sweepPick").onchange = () => { follow = false; $("follow").checked = false; openSweep($("sweepPick").value, sweeps.find((s) => s.id === $("sweepPick").value)); };
$("follow").onchange = (e) => { follow = e.target.checked; if (follow) loadSweeps(); };
setInterval(loadSweeps, 4000);

function hero() {
  const s = sweeps.find((x) => x.id === sweepId); if (!s) return;
  const eb = $("heroEyebrow");
  eb.textContent = s.live ? "Live sweep" : s.packet ? "Completed sweep" : "Sweep";
  eb.classList.toggle("live", !!s.live);
  $("heroTitle").textContent = when(s);
  $("heroMeta").replaceChildren(...[s.country && `Country ${s.country}`, `Currency ${currency}`, plural(s.frames, "frame"), `ID ${s.id}`]
    .filter(Boolean).map((t) => el("span", null, t)));
}

// ---------------------------------------------------------------- open a sweep
function reset() {
  inv = { books: [], items: [], counts: {} }; review = []; seenIds = new Set();
  $("feed").replaceChildren(); $("convo").replaceChildren(); bubbles.clear(); emptyStates();
  $("packet").hidden = true; $("packetActions").hidden = true;
  $("kRoom").textContent = "—"; $("kRoomSub").textContent = "not measured";
  $("kValue").textContent = "—"; $("kValueSub").textContent = "after the packet is built";
  closeDrawer(); render();
}

function openSweep(id, info) {
  if (!id) return;
  sweepId = id; reset(); hero();
  if (ws) { ws.onclose = null; ws.close(); }
  connect();
  if (info?.packet) loadPacket();
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws", id = sweepId;
  ws = new WebSocket(`${proto}://${location.host}/ws/live?${q({ sweep: id, role: "dashboard" })}`);
  ws.onopen = () => pill("pillConn", "Server connected", "ok");
  ws.onclose = () => { pill("pillConn", "Reconnecting…", "warn"); setTimeout(() => sweepId === id && connect(), 2000); };
  ws.onmessage = (ev) => handle(JSON.parse(ev.data));
}

// ---------------------------------------------------------------- live events
function handle(m) {
  switch (m.type) {
    case "hello":
      currency = m.meta?.currency || "USD"; hero();
      pill("pillPhone", m.phone ? "Phone connected" : "Phone offline", m.phone ? "ok" : "off");
      phoneOnline = m.phone; showConnect();
      if (m.finished) loadPacket();
      break;
    case "phone":
      pill("pillPhone", m.connected ? "Phone connected" : "Phone offline", m.connected ? "ok" : "off");
      phoneOnline = m.connected; showConnect();
      feed("phone", m.connected ? "<b>Phone connected</b>" : "Phone disconnected"); break;
    case "agent_state": { const [t, s] = VOICE[m.state] || ["Voice " + m.state, "warn"]; pill("pillVoice", t, s); break; }
    case "inventory": inv = m; render(); break;
    case "frame": frameEvent(m); break;
    case "coach": feed("coach", "<b>Coach</b> " + esc(m.ask || m.text)); break;
    case "status": feed("status", esc(m.text)); break;
    case "room": roomKpi(m); feed("room", `<b>Room measured</b> ${esc(m.floor_area_m2 ?? "")} m²`); break;
    case "transcript": caption(m); break;
    case "packet_ready": feed("packet", "<b>Claim packet ready</b>"); loadPacket(m); break;
  }
}

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

function frameEvent(m) {
  const f = esc(m.frame_id);
  if (m.error) return feed("error", `<b>${f}</b> analysis failed: ${esc(m.error)}`, m.t);
  const skipped = m.skipped ? ` · ${plural(m.skipped, "older frame")} queued for the end` : "";
  if (m.carried) return feed("carried", `<b>${f}</b> already analysed, results reused${skipped}`, m.t);
  const titles = m.titles?.length ? ` — ${esc(m.titles.join(", "))}` : "";
  feed("frame", `<b>${f}</b> ${(m.ms / 1000).toFixed(1)} s · ${plural(m.books, "book")} (${m.identified} identified${titles}) · ` +
       `${plural(m.items, "item")}${skipped}`, m.t);
}

function feed(kind, html, t) {
  const li = el("li", "new"); li.dataset.k = kind;
  const what = el("span", "what"); what.innerHTML = html;                 // callers escape all untrusted text
  li.append(what, el("time", null, t != null ? clock(t) : new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })));
  const list = $("feed"); list.prepend(li);
  while (list.children.length > 300) list.lastChild.remove();
  emptyStates();
}

function emptyStates() {
  $("feedEmpty").hidden = $("feed").children.length > 0;
  $("feedCount").textContent = $("feed").children.length ? plural($("feed").children.length, "event") : "";
  $("convoEmpty").hidden = $("convo").children.length > 0;
}

function roomKpi(r) {
  if (r.floor_area_m2 == null) return;
  $("kRoom").textContent = `${r.floor_area_m2} m²`;
  $("kRoomSub").textContent = `${r.length_m} × ${r.width_m} m${r.height_m ? ` × ${r.height_m} m high` : ""}`;
}

// ---------------------------------------------------------------- conversation
const bubbles = new Map();
function caption(m) {
  const box = $("convo");
  let b = bubbles.get(m.item_id);
  if (!b) {
    b = el("div", `msg ${m.role}`);
    b.append(el("span", "av", m.role === "agent" ? "AI" : "You"), el("div", "bubble"));
    bubbles.set(m.item_id, b); box.append(b);
  }
  const body = b.querySelector(".bubble");
  body.textContent = m.mode === "append" ? body.textContent + m.text : m.text;
  b.classList.toggle("pending", m.role === "user" && !body.textContent);
  box.scrollTop = box.scrollHeight;
  emptyStates();
}

// ---------------------------------------------------------------- packet
async function loadPacket(m) {
  const r = await api(`/api/sweeps/${sweepId}/packet`); if (!r.ok) return;
  const p = await r.json(); currency = p.currency || currency;
  review = p.review_queue || [];
  const t = p.totals;
  $("pSummary").textContent = m?.summary || `${plural(t.book_count, "book")}, ${plural(t.item_count, "other item")}, ${plural(review.length, "line")} to review.`;
  $("pTotals").replaceChildren(...[
    [money(t.books_replacement_cost, currency), "Books · replacement"], [money(t.books_used_value, currency), "Books · used value"],
    [`${money(t.items_replacement_cost_low, currency)} – ${money(t.items_replacement_cost_high, currency)}`, "Other items"],
    [t.excluded_from_totals, "Lines without a price"], [review.length, "Lines to review"],
  ].map(([v, s]) => { const d = el("div"); d.append(el("b", null, String(v)), el("span", null, s)); return d; }));
  $("pReport").href = `/api/sweeps/${sweepId}/report?${q({})}`; $("pJson").href = `/api/sweeps/${sweepId}/packet?${q({})}`;
  $("packet").hidden = false; $("packetActions").hidden = false;
  $("kValue").textContent = money(t.books_replacement_cost, currency);
  $("kValueSub").textContent = `books; items ${money(t.items_replacement_cost_low, currency)}–${money(t.items_replacement_cost_high, currency)}`;
  if (p.room) roomKpi(p.room);
  hero(); render();
}

// ---------------------------------------------------------------- records table
function thumb(r) {
  const td = el("td", "thumb");
  if (r.frame && r.bbox) { const img = el("img"); img.loading = "lazy"; img.alt = ""; img.src = crop(r, 128); td.append(img); }
  return td;
}
function cell(...children) { const td = el("td"); td.append(...children); return td; }
const num = (t) => el("span", "num", t);
function stack(title, sub) { const d = el("div"); d.append(el("div", "cell-title", title)); if (sub) d.append(el("div", "cell-sub", sub)); return d; }
function badge(status) {
  const cls = status === "identified" ? "ok" : status === "needs_appraisal" ? "bad" : "un";
  return el("span", `status-badge ${cls}`, words(status || "pending"));
}
function meter(v) {
  if (v == null) return num("—");
  const d = el("span", "meter"), i = el("i"); i.style.setProperty("--v", `${Math.round(v * 100)}%`);
  d.append(i, num(`${Math.round(v * 100)}%`)); return d;
}

// columns: [label, sort key or null]
function head(cols) {
  const tr = el("tr");
  cols.forEach(([label, key]) => {
    const th = el("th");
    if (!key) { th.className = "static"; th.textContent = label; }
    else {
      const b = el("button", null, label); b.type = "button";
      b.onclick = () => { sort = { key, dir: sort.key === key ? -sort.dir : 1 }; render(); };
      th.append(b);
      if (sort.key === key) th.setAttribute("aria-sort", sort.dir > 0 ? "ascending" : "descending");
    }
    tr.append(th);
  });
  $("table").tHead.replaceChildren(tr);
}
const match = (r, needle) => !needle || Object.values(r).some((v) => typeof v === "string" && v.toLowerCase().includes(needle));
function sorted(list, keys) {
  if (!sort.key || !keys.includes(sort.key)) return list;
  const k = sort.key;
  return [...list].sort((a, b) => {
    const x = a[k] ?? "", y = b[k] ?? "";
    return (typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y))) * sort.dir;
  });
}

function render() {
  const c = inv.counts || {}, books = inv.books || [], items = inv.items || [];
  const nb = c.books ?? 0, ni = c.identified ?? 0;
  $("kBooks").textContent = nb;
  $("kIdentSub").textContent = `${ni} identified${nb ? ` · ${Math.round((ni / nb) * 100)}%` : ""}`;
  $("kIdentBar").style.width = nb ? `${(ni / nb) * 100}%` : "0";
  $("kUnread").textContent = nb - ni; $("kItems").textContent = items.length;
  $("kFrames").textContent = c.frames ?? 0; $("kFramesSub").textContent = `${c.analysed ?? 0} analysed`;
  $("cBooks").textContent = books.length; $("cItems").textContent = items.length; $("cReview").textContent = review.length;
  $("filters").hidden = tab !== "books";
  const needle = $("search").value.trim().toLowerCase(), body = $("table").tBodies[0];
  let rows = [], empty;
  if (tab === "books") {
    head([["", null], ["Book", "title"], ["Status", "status"], ["Shelf", "shelf"], ["Seen", "seen"], ["Match", "confidence"]]);
    const list = books.filter((b) => (filter === "all" || (filter === "identified") === (b.status === "identified")) && match(b, needle));
    rows = sorted(list, ["title", "status", "shelf", "seen", "confidence"]).map((b) => {
      const ident = b.status === "identified";
      const tr = row(b, "book");
      tr.append(thumb(b), cell(stack(ident ? b.title : "Unidentified book",
                  ident ? [b.author, b.edition].filter(Boolean).join(" · ") : words(b.reason || "not read yet"))),
                cell(badge(b.status)), cell(num(b.shelf ? b.shelf.split(":").pop() : "—")),
                cell(num(plural(b.seen, "frame"))), cell(meter(b.confidence)));
      return tr;
    });
    empty = books.length ? "No books match your search or filter." : "No books yet. They appear here as the phone pans the shelves.";
  } else if (tab === "items") {
    head([["", null], ["Item", "category"], ["Material", "material"], ["Brand / model", "brand_model"], ["Seen", "seen"]]);
    rows = sorted(items.filter((i) => match(i, needle)), ["category", "material", "brand_model", "seen"]).map((i) => {
      const tr = row(i, "item");
      tr.append(thumb(i), cell(stack(words(i.category || "unknown item"))), cell(num(i.material || "—")),
                cell(num(i.brand_model || "—")), cell(num(plural(i.seen, "frame"))));
      return tr;
    });
    empty = items.length ? "No items match your search." : "No other items yet.";
  } else {
    head([["Line", "ref_id"], ["Reason", "reason"], ["Detail", null]]);
    rows = sorted(review.filter((r) => match(r, needle)), ["ref_id", "reason"]).map((r) => {
      const tr = el("tr");
      tr.append(cell(el("span", "cell-title", r.ref_id)), cell(el("span", "status-badge un", words(r.reason))), cell(el("span", "cell-sub", words(r.detail || ""))));
      return tr;
    });
    empty = review.length ? "No review lines match your search." : "The review queue is built when the sweep finishes (Finish on the phone).";
  }
  rows.forEach((tr) => { if (tr.dataset.id && !seenIds.has(tr.dataset.id)) { tr.classList.add("new"); seenIds.add(tr.dataset.id); } });
  body.replaceChildren(...rows);
  $("emptyText").textContent = empty;
  $("empty").hidden = rows.length > 0;
}

function row(r, kind) {
  const tr = el("tr"); tr.dataset.id = r.id; tr.tabIndex = 0;
  tr.onclick = () => openDrawer(r, kind);
  tr.onkeydown = (e) => { if (e.key === "Enter") openDrawer(r, kind); };
  return tr;
}

document.querySelectorAll("[role=tab]").forEach((b) => (b.onclick = () => {
  tab = b.dataset.tab; sort = { key: null, dir: 1 };
  document.querySelectorAll("[role=tab]").forEach((x) => x.setAttribute("aria-selected", x === b)); render();
}));
document.querySelectorAll("#filters button").forEach((b) => (b.onclick = () => {
  filter = b.dataset.f; document.querySelectorAll("#filters button").forEach((x) => x.setAttribute("aria-pressed", x === b)); render();
}));
$("search").oninput = render;

// ---------------------------------------------------------------- record detail drawer
function openDrawer(r, kind) {
  const ident = r.status === "identified";
  $("dTitle").textContent = kind === "book" ? (ident ? r.title : "Unidentified book") : words(r.category || "Item");
  const img = $("dImg"); img.hidden = !(r.frame && r.bbox); if (!img.hidden) img.src = crop(r, 600);
  const fields = kind === "book"
    ? [["Status", badge(r.status)], ["Title", r.title], ["Author", r.author], ["Edition", r.edition], ["Reason", r.reason && words(r.reason)],
       ["Shelf", r.shelf], ["Seen in", plural(r.seen, "frame")], ["Match", r.confidence != null ? `${Math.round(r.confidence * 100)}%` : null],
       ["Source frame", r.frame], ["Record ID", r.id]]
    : [["Category", words(r.category)], ["Material", r.material], ["Brand / model", r.brand_model], ["Seen in", plural(r.seen, "frame")],
       ["Source frame", r.frame], ["Record ID", r.id]];
  $("dFields").replaceChildren(...fields.filter(([, v]) => v != null && v !== "").flatMap(([k, v]) => {
    const dd = el("dd"); typeof v === "string" ? (dd.textContent = v) : dd.append(v);
    return [el("dt", null, k), dd];
  }));
  $("drawer").hidden = false; $("scrim").hidden = false; $("dClose").focus();
}
function closeDrawer() { $("drawer").hidden = true; $("scrim").hidden = true; }
$("dClose").onclick = closeDrawer; $("scrim").onclick = closeDrawer;
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

// ---------------------------------------------------------------- connect the phone (QR lives here, not in the terminal)
async function loadConnect() {
  const r = await api("/api/connect"); if (!r.ok) return;
  const c = await r.json();
  $("qr").innerHTML = c.qr_svg;                       // SVG generated by our own server for the phone link
  $("phoneUrl").textContent = c.phone_url; $("httpsWarn").hidden = c.https;
  $("copyUrl").onclick = () => navigator.clipboard?.writeText(c.phone_url).then(() => ($("copyUrl").textContent = "Copied"));
}
function showConnect(force) { $("connect").hidden = !(force || (!phoneOnline && !qrDismissed)); }
$("showQr").onclick = () => { qrDismissed = false; showConnect(true); };
$("hideQr").onclick = () => { qrDismissed = true; showConnect(); };

emptyStates();
if (!TOKEN) pill("pillConn", "Open this page from the link the server printed", "bad");
else { loadConnect(); showConnect(); loadSweeps(); }
