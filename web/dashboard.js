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

let sweepId = null, ws = null, follow = true, currency = "USD";
let inv = { books: [], items: [], counts: {} }, review = [], tab = "books", filter = "all", seenIds = new Set();

// ---------------------------------------------------------------- pills
function pill(id, text, s) { const p = $(id); p.dataset.s = s; p.querySelector("span").textContent = text; }
const VOICE = { listening: ["Agent listening", "ok"], hearing: ["Hearing the user", "live"], thinking: ["Agent thinking", "warn"],
                speaking: ["Agent speaking", "ok"] };

// ---------------------------------------------------------------- sweep picker (+ follow the newest live sweep)
async function loadSweeps() {
  const r = await api("/api/sweeps"); if (!r.ok) { pill("pillConn", r.status === 401 ? "Wrong link: reopen from the terminal" : "Server error", "bad"); return; }
  const list = await r.json(), pick = $("sweepPick"), current = pick.value;
  pick.replaceChildren(...list.map((s) => {
    const when = s.created_at ? new Date(s.created_at * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" }) : s.id;
    return new Option(`${s.live ? "● LIVE · " : ""}${when} · ${s.country || "?"} · ${s.frames} frames${s.packet ? " · packet" : ""}`, s.id);
  }));
  if (!list.length) { pick.append(new Option("No sweeps yet", "")); return; }
  const target = follow ? (list.find((s) => s.live) || list[0]).id : (current || list[0].id);
  pick.value = target;
  if (target !== sweepId) openSweep(target, list.find((s) => s.id === target));
}
$("sweepPick").onchange = () => { follow = false; $("follow").checked = false; openSweep($("sweepPick").value); };
$("follow").onchange = (e) => { follow = e.target.checked; if (follow) loadSweeps(); };
setInterval(loadSweeps, 4000);

// ---------------------------------------------------------------- open a sweep
function reset() {
  inv = { books: [], items: [], counts: {} }; review = []; seenIds = new Set();
  $("feed").replaceChildren(); $("convo").replaceChildren(el("p", "empty", "Nothing said yet.")); bubbles.clear();
  $("packet").hidden = true; ["kRoom", "kValue"].forEach((k) => ($(k).textContent = "—"));
  $("kRoomSub").textContent = "room (not measured)"; $("kValueSub").textContent = "replacement value";
  render();
}

function openSweep(id, info) {
  if (!id) return;
  sweepId = id; reset();
  if (ws) { ws.onclose = null; ws.close(); }
  connect();
  if (info?.packet) loadPacket();
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws", id = sweepId;
  ws = new WebSocket(`${proto}://${location.host}/ws/live?${q({ sweep: id, role: "dashboard" })}`);
  ws.onopen = () => pill("pillConn", "Connected", "ok");
  ws.onclose = () => { pill("pillConn", "Reconnecting…", "warn"); setTimeout(() => sweepId === id && connect(), 2000); };
  ws.onmessage = (ev) => handle(JSON.parse(ev.data));
}

// ---------------------------------------------------------------- live events
function handle(m) {
  switch (m.type) {
    case "hello":
      currency = m.meta?.currency || "USD";
      pill("pillPhone", m.phone ? "Phone connected" : "Phone offline", m.phone ? "ok" : "off");
      if (m.finished) loadPacket();
      break;
    case "phone":
      pill("pillPhone", m.connected ? "Phone connected" : "Phone offline", m.connected ? "ok" : "off");
      feed(m.connected ? "📱" : "📴", m.connected ? "Phone connected" : "Phone disconnected"); break;
    case "agent_state": { const [t, s] = VOICE[m.state] || ["Voice " + m.state, "warn"]; pill("pillVoice", t, s); break; }
    case "inventory": inv = m; render(); break;
    case "frame": frameEvent(m); break;
    case "coach": feed("💡", "Coach: " + (m.ask || m.text)); break;
    case "status": feed("ℹ️", m.text); break;
    case "room": roomKpi(m); break;
    case "transcript": caption(m); break;
    case "packet_ready": feed("✅", "Claim packet ready"); loadPacket(m); break;
  }
}

function frameEvent(m) {
  if (m.error) return feed("⚠️", `${m.frame_id}: analysis failed (${m.error})`);
  const skipped = m.skipped ? ` · ${m.skipped} older frame${m.skipped > 1 ? "s" : ""} queued for the end` : "";
  if (m.carried) return feed("↻", `${m.frame_id}: view already analysed, results copied (no model calls)${skipped}`, m.t);
  const titles = m.titles?.length ? `: ${m.titles.join(", ")}` : "";
  feed("📷", `${m.frame_id} analysed in ${(m.ms / 1000).toFixed(1)} s · ${m.books} book${m.books === 1 ? "" : "s"} ` +
       `(${m.identified} identified${titles}) · ${m.items} item${m.items === 1 ? "" : "s"}${skipped}`, m.t);
}

function feed(icon, text, t) {
  const li = el("li", "new"), now = new Date();
  li.append(el("time", null, t != null ? clock(t) : now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })), el("span", null, `${icon} ${text}`));
  const list = $("feed"); list.prepend(li);
  while (list.children.length > 300) list.lastChild.remove();
}

function roomKpi(r) {
  if (r.floor_area_m2 == null) return;
  $("kRoom").textContent = `${r.floor_area_m2} m²`;
  $("kRoomSub").textContent = `room ${r.length_m} × ${r.width_m} m${r.height_m ? ` × ${r.height_m} m` : ""}`;
}

// ---------------------------------------------------------------- conversation
const bubbles = new Map();
function caption(m) {
  const box = $("convo"); box.querySelector(".empty")?.remove();
  let b = bubbles.get(m.item_id);
  if (!b) { b = el("div", `msg ${m.role}`); b.append(el("small", null, m.role === "agent" ? "Assistant" : "User"), el("span")); bubbles.set(m.item_id, b); box.append(b); }
  const body = b.querySelector("span");
  body.textContent = m.mode === "append" ? body.textContent + m.text : m.text;
  b.classList.toggle("pending", m.role === "user" && !body.textContent);
  box.scrollTop = box.scrollHeight;
}

// ---------------------------------------------------------------- packet
async function loadPacket(m) {
  const r = await api(`/api/sweeps/${sweepId}/packet`); if (!r.ok) return;
  const p = await r.json(); currency = p.currency || currency;
  review = p.review_queue || [];
  const t = p.totals;
  $("pSummary").textContent = m?.summary || `${t.book_count} books, ${t.item_count} other items, ${review.length} lines to review.`;
  $("pTotals").replaceChildren(...[
    [money(t.books_replacement_cost, currency), "books, replacement"], [money(t.books_used_value, currency), "books, used value"],
    [`${money(t.items_replacement_cost_low, currency)} – ${money(t.items_replacement_cost_high, currency)}`, "other items"],
    [t.excluded_from_totals, "lines without a counted price"], [review.length, "lines to review"],
  ].map(([v, s]) => { const d = el("div"); d.append(el("b", null, String(v)), el("span", null, s)); return d; }));
  $("pReport").href = `/api/sweeps/${sweepId}/report?${q({})}`; $("pJson").href = `/api/sweeps/${sweepId}/packet?${q({})}`;
  $("packet").hidden = false;
  $("kValue").textContent = money(t.books_replacement_cost, currency);
  if (p.room) roomKpi(p.room);
  render();
}

// ---------------------------------------------------------------- records table
function thumb(r) {
  const td = el("td", "thumb");
  if (r.frame && r.bbox) {
    const img = el("img"); img.loading = "lazy"; img.alt = "";
    img.src = `/api/sweeps/${sweepId}/crop?${q({ f: r.frame, b: r.bbox.join(","), w: 128 })}`;
    img.onclick = () => zoom(`/api/sweeps/${sweepId}/crop?${q({ f: r.frame, b: r.bbox.join(","), w: 400 })}`);
    td.append(img);
  }
  return td;
}
function zoom(src) { const z = el("div"); z.id = "zoom"; const i = el("img"); i.src = src; z.append(i); z.onclick = () => z.remove(); document.body.append(z); }
function cell(...children) { const td = el("td"); td.append(...children); return td; }
function head(...cols) { const tr = el("tr"); cols.forEach((c) => tr.append(el("th", null, c))); $("table").tHead.replaceChildren(tr); }
const match = (r, needle) => !needle || Object.values(r).some((v) => typeof v === "string" && v.toLowerCase().includes(needle));

function render() {
  const c = inv.counts || {}, books = inv.books || [], items = inv.items || [];
  $("kBooks").textContent = c.books ?? 0; $("kIdent").textContent = c.identified ?? 0;
  $("kUnread").textContent = (c.books ?? 0) - (c.identified ?? 0); $("kItems").textContent = items.length;
  $("kFrames").textContent = c.frames ?? 0; $("kFramesSub").textContent = `frames (${c.analysed ?? 0} analysed)`;
  $("cBooks").textContent = books.length; $("cItems").textContent = items.length; $("cReview").textContent = review.length;
  $("filters").hidden = tab !== "books";
  const needle = $("search").value.trim().toLowerCase(), body = $("table").tBodies[0];
  let rows = [];
  if (tab === "books") {
    head("", "Book", "Status", "Shelf", "Seen in", "Match");
    rows = books.filter((b) => (filter === "all" || (filter === "identified") === (b.status === "identified")) && match(b, needle)).map((b) => {
      const ident = b.status === "identified";
      const main = el("div", "title", ident ? b.title : "Unidentified book");
      const sub = el("div", "sub", ident ? [b.author, b.edition].filter(Boolean).join(" · ") : words(b.reason || "not read yet"));
      const tr = el("tr"); tr.dataset.id = b.id;
      tr.append(thumb(b), cell(main, sub), cell(el("span", `tag ${ident ? "ok" : b.status === "needs_appraisal" ? "bad" : "un"}`, words(b.status))),
                cell(el("span", "num", b.shelf ? b.shelf.split(":").pop() : "—")), cell(el("span", "num", `${b.seen} frame${b.seen === 1 ? "" : "s"}`)),
                cell(el("span", "num", b.confidence != null ? `${Math.round(b.confidence * 100)}%` : "—")));
      return tr;
    });
    $("empty").textContent = books.length ? "No books match." : "No books yet. They appear here as the phone pans the shelves.";
  } else if (tab === "items") {
    head("", "Item", "Material", "Brand / model", "Seen in");
    rows = items.filter((i) => match(i, needle)).map((i) => {
      const tr = el("tr"); tr.dataset.id = i.id;
      tr.append(thumb(i), cell(el("div", "title", words(i.category || "unknown item"))), cell(el("span", "num", i.material || "—")),
                cell(el("span", "num", i.brand_model || "—")), cell(el("span", "num", `${i.seen} frame${i.seen === 1 ? "" : "s"}`)));
      return tr;
    });
    $("empty").textContent = items.length ? "No items match." : "No other items yet.";
  } else {
    head("Line", "Reason", "Detail");
    rows = review.filter((r) => match(r, needle)).map((r) => {
      const tr = el("tr");
      tr.append(cell(el("span", "title", r.ref_id)), cell(el("span", "tag un", words(r.reason))), cell(el("span", "sub", words(r.detail || ""))));
      return tr;
    });
    $("empty").textContent = "The review queue is built when the sweep finishes (Finish on the phone).";
  }
  rows.forEach((tr) => { if (tr.dataset.id && !seenIds.has(tr.dataset.id)) { tr.classList.add("new"); seenIds.add(tr.dataset.id); } });
  body.replaceChildren(...rows);
  $("empty").hidden = rows.length > 0;
}

document.querySelectorAll("[role=tab]").forEach((b) => (b.onclick = () => {
  tab = b.dataset.tab; document.querySelectorAll("[role=tab]").forEach((x) => x.setAttribute("aria-selected", x === b)); render();
}));
document.querySelectorAll("#filters button").forEach((b) => (b.onclick = () => {
  filter = b.dataset.f; document.querySelectorAll("#filters button").forEach((x) => x.setAttribute("aria-pressed", x === b)); render();
}));
$("search").oninput = render;

if (!TOKEN) pill("pillConn", "Open this page from the link the server printed", "bad");
else loadSweeps();
