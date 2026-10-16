// Phone capture app. Sections: config, helpers, ui, keyframes, voice (WebRTC), websocket, start/finish.
// Server contract: backend/live.py (HTTP + /ws/live) and backend/agent.py (message types). Tunables: CONFIG.
"use strict";
const CONFIG = {
  keyframeEveryMs: 700,      // how often we consider sending a hi-res keyframe (consecutive frames must overlap for tracking)
  video: { width: 3840, height: 2160 },   // requested camera resolution; the phone picks the closest it supports
  sharpFloor: 30,            // Laplacian variance on the 320px copy; below this we skip the frame
  diffFloor: 8,              // mean abs diff vs last sent keyframe (0..255); below = same view
  jpegQuality: 0.82, jpegQualityLow: 0.6,
  maxSide: 2560,             // long side of uploaded keyframes, px (spine text stays readable; ~1 MB per frame)
  maxBytes: 2_500_000,       // stay under the server's keyframe_max_bytes (3 MB, backend/config.py)
  queueMax: 20, queueSlow: 10, maxKeyframes: 400,
  coachBannerMs: 8000,
};
const $ = (id) => document.getElementById(id);
const TOKEN = new URLSearchParams(location.search).get("t") || "";
const STATES = { listening: "Listening", hearing: "Hearing you…", thinking: "Thinking…", speaking: "Speaking", muted: "Muted", connecting: "Connecting…", ptt: "Hold the button to talk", off: "Voice off" };

let sweepId, ws, stream, seq = 0, t0 = 0, wakeLock = null, finished = false, muted = false, agentState = "connecting";
let queue = [], sending = false, quality = CONFIG.jpegQuality, interval = CONFIG.keyframeEveryMs, lastSmall = null;
const video = $("cam");

// ---------------------------------------------------------------- helpers
const api = (path, opts = {}) => fetch(path, { ...opts, headers: { "X-Sweep-Token": TOKEN, ...(opts.headers || {}) } });
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

// ---------------------------------------------------------------- ui
function setPill(text, s = "idle") { $("pillText").textContent = text; $("pill").dataset.s = s; }
function setAgent(state) {
  agentState = state;
  const shown = !micOpen() && state !== "speaking" ? (talkMode === "ptt" ? "ptt" : "muted") : state;
  $("orb").dataset.s = shown; $("agentState").textContent = STATES[shown] || shown;
}
let coachTimer;
function showCoach(text) {
  const c = $("coach"); c.textContent = text; c.hidden = false;
  clearTimeout(coachTimer); coachTimer = setTimeout(() => (c.hidden = true), CONFIG.coachBannerMs);
}
function addSystemLine(text) { const p = el("div", "sys", text); const t = $("transcript"); t.querySelector(".empty")?.remove(); t.append(p); stick(t); }
function stick(panel) { panel.scrollTop = panel.scrollHeight; }

// transcript bubbles, keyed by item id so streamed text lands in the right bubble
const bubbles = new Map();
function caption(m) {
  const panel = $("transcript"); panel.querySelector(".empty")?.remove();
  let b = bubbles.get(m.item_id);
  if (!b) {
    b = el("div", `msg ${m.role}`); b.append(el("small", null, m.role === "agent" ? "Assistant" : "You"), el("span"));
    bubbles.set(m.item_id, b); panel.append(b);
  }
  const body = b.querySelector("span");
  body.textContent = m.mode === "set" && m.text === "" && !body.textContent ? "" : (m.mode === "append" ? body.textContent + m.text : m.text);
  b.classList.toggle("pending", m.role === "user" && !body.textContent);
  if (m.role === "agent") $("agentLine").textContent = body.textContent;
  stick(panel);
}

function renderInventory(m) {
  $("chipBooks").textContent = `${m.counts.books} book${m.counts.books === 1 ? "" : "s"}`;
  $("chipItems").textContent = `${m.items.length} item${m.items.length === 1 ? "" : "s"}`;
  $("tabCount").textContent = m.counts.books + m.items.length;
  const books = $("books"), items = $("items"); books.replaceChildren(); items.replaceChildren();
  m.books.forEach((b, i) => {
    const li = el("li"); li.append(el("span", null, b.title || `Unidentified book #${i + 1}`));
    li.append(el("span", `tag ${b.status === "identified" ? "ok" : "un"}`, b.status === "identified" ? "identified" : (b.reason || "unidentified").replace(/_/g, " ")));
    books.append(li);
  });
  m.items.forEach((x) => { const li = el("li"); li.append(el("span", null, x.category || "item")); items.append(li); });
  if (!m.books.length) books.append(el("li", null, "Nothing yet."));
  if (!m.items.length) items.append(el("li", null, "Nothing yet."));
}

function selectTab(name) {
  const t = name === "transcript";
  $("tabT").setAttribute("aria-selected", t); $("tabI").setAttribute("aria-selected", !t);
  $("transcript").hidden = !t; $("inventory").hidden = t; if (t) stick($("transcript"));
}
$("tabT").onclick = () => selectTab("transcript"); $("tabI").onclick = () => selectTab("inventory");

// ---------------------------------------------------------------- quality + keyframes (hi-res, to disk on the server)
// Quality scores: same formulas as backend/quality.py, run on a 320px grayscale copy.
function scoreFrame(v) {
  const w = 320, h = Math.round((v.videoHeight / v.videoWidth) * w) || 180;
  const c = scoreFrame.c || (scoreFrame.c = document.createElement("canvas")); c.width = w; c.height = h;
  const g = c.getContext("2d", { willReadFrequently: true }); g.drawImage(v, 0, 0, w, h);
  const d = g.getImageData(0, 0, w, h).data, gray = new Float32Array(w * h);
  let sum = 0, glare = 0;
  for (let i = 0; i < w * h; i++) { const y = 0.299 * d[4 * i] + 0.587 * d[4 * i + 1] + 0.114 * d[4 * i + 2]; gray[i] = y; sum += y; if (y > 250) glare++; }
  let m = 0, m2 = 0, n = 0;
  for (let y = 1; y < h - 1; y++) for (let x = 1; x < w - 1; x++) {
    const i = y * w + x, l = gray[i - 1] + gray[i + 1] + gray[i - w] + gray[i + w] - 4 * gray[i];
    m += l; m2 += l * l; n++;
  }
  const blur = m2 / n - (m / n) ** 2;
  let diff = 255;
  if (lastSmall) { diff = 0; for (let i = 0; i < gray.length; i += 7) diff += Math.abs(gray[i] - lastSmall[i]); diff /= gray.length / 7; }
  return { blur, glare: glare / (w * h), dark: sum / (w * h), diff, gray };
}

// Keyframes come from the live VIDEO stream (not ImageCapture.takePhoto(), which refocuses and fires the shutter
// on every call: ~2.6 s per frame and a frozen preview on real phones). The frame is grabbed as an ImageBitmap
// (cheap, GPU) and JPEG-encoded in a Web Worker, so the preview and UI never stall. Long side <= maxSide and the
// file is shrunk until it fits maxBytes. Returns {blob, w, h} with the real encoded size.
const ENCODER_SRC = `onmessage = async (e) => {
  const { bmp, maxSide, maxBytes } = e.data; let q = e.data.q, c, blob;
  let scale = Math.min(1, maxSide / Math.max(bmp.width, bmp.height));
  for (let a = 0; a < 5; a++) {
    c = new OffscreenCanvas(Math.round(bmp.width * scale), Math.round(bmp.height * scale));
    c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
    blob = await c.convertToBlob({ type: "image/jpeg", quality: q });
    if (blob.size <= maxBytes) break;
    q = Math.max(0.5, q - 0.1); scale *= 0.85;
  }
  bmp.close(); postMessage({ blob, w: c.width, h: c.height });
};`;
let encoder = null;
try { if (window.OffscreenCanvas && window.Worker) encoder = new Worker(URL.createObjectURL(new Blob([ENCODER_SRC], { type: "text/javascript" }))); } catch (e) { encoder = null; }

async function grabJpeg(q) {
  const bmp = await createImageBitmap(video);
  if (encoder) {                                   // off the main thread
    return await new Promise((resolve) => {
      encoder.onmessage = (e) => resolve(e.data);
      encoder.postMessage({ bmp, q, maxSide: CONFIG.maxSide, maxBytes: CONFIG.maxBytes }, [bmp]);
    });
  }
  const c = grabJpeg.c || (grabJpeg.c = document.createElement("canvas"));   // fallback: older browsers
  let scale = Math.min(1, CONFIG.maxSide / Math.max(bmp.width, bmp.height)), blob = null;
  for (let attempt = 0; attempt < 5; attempt++) {
    c.width = Math.round(bmp.width * scale); c.height = Math.round(bmp.height * scale);
    c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
    blob = await new Promise((r) => c.toBlob(r, "image/jpeg", q));
    if (blob && blob.size <= CONFIG.maxBytes) break;
    q = Math.max(0.5, q - 0.1); scale *= 0.85;
  }
  bmp.close();
  return { blob, w: c.width, h: c.height };
}

async function keyframeTick() {
  if (!sweepId || finished || xr || seq >= CONFIG.maxKeyframes || video.readyState < 2) return;   // paused while measuring
  const s = scoreFrame(video);
  if (s.blur < CONFIG.sharpFloor || s.diff < CONFIG.diffFloor) return;
  lastSmall = s.gray;
  const { blob, w, h } = await grabJpeg(quality);
  if (!blob) return;
  if (queue.length >= CONFIG.queueMax) { console.warn("upload queue full: dropping oldest keyframe"); queue.shift(); }
  queue.push({ seq: seq++, t: (performance.now() - t0) / 1000, blob, w, h, ...s });
  if (queue.length > CONFIG.queueSlow) {           // adapt: lower quality + slow down
    quality = CONFIG.jpegQualityLow; interval = CONFIG.keyframeEveryMs * 2; setPill("Upload slow: go gently", "warn");
  } else { quality = CONFIG.jpegQuality; interval = CONFIG.keyframeEveryMs; }
  pump();
}

async function pump() {                            // frames stay queued until the server acks (resent after reconnect)
  if (sending) return; sending = true;
  while (queue.length) {
    const f = queue[0];
    try {
      const q = new URLSearchParams({ seq: f.seq, t: f.t.toFixed(2), w: f.w, h: f.h, blur: f.blur.toFixed(1), glare: f.glare.toFixed(4), dark: f.dark.toFixed(1) });
      const r = await api(`/api/sweeps/${sweepId}/frames?${q}`, { method: "POST", body: f.blob, headers: { "Content-Type": "image/jpeg" } });
      if (r.status === 413) {                     // should not happen after grabJpeg's cap; never drop silently
        console.error(`keyframe ${f.seq} rejected as too large (${(f.blob.size / 1e6).toFixed(1)} MB)`);
        setPill("A photo was too large", "warn");
      } else if (!r.ok) throw new Error(r.status);
      queue.shift();
    } catch (e) { console.warn("upload retry:", e.message || e); await new Promise((r) => setTimeout(r, 1500)); }
  }
  sending = false;
}
function scheduleKeyframes() { keyframeTick().finally(() => sweepId && !finished && setTimeout(scheduleKeyframes, interval)); }

// ---------------------------------------------------------------- voice: WebRTC straight to OpenAI
// The server only relays the SDP offer/answer (POST /api/sweeps/<id>/voice) and drives the agent "sideband".
// Over WebRTC the browser applies full echo cancellation + noise suppression (like a video call), so the
// agent no longer hears itself or the room the way it did with raw audio over our WebSocket.
let pc = null, actx = null, greeted = false, voiceRetry = 0, talkMode = "open", holding = false;
const agentAudio = $("agentAudio"), micBtn = $("mute");
const micTrack = () => stream?.getAudioTracks()[0];
const micOpen = () => (talkMode === "open" ? !muted : holding);

// Must run SYNCHRONOUSLY inside the Start tap (before any await): phones only allow audio that a tap started.
function unlockAudio() {
  actx = new AudioContext(); actx.resume();
  agentAudio.muted = false; agentAudio.play().catch(() => {});      // primes <audio> so the agent's voice can autoplay later
}
function ensureSoundOn() {                          // fallback if the browser still blocked playback
  agentAudio.play().catch(() => {
    showCoach("Tap anywhere to turn on the assistant's voice");
    document.addEventListener("click", () => { agentAudio.play(); actx.resume(); $("coach").hidden = true; }, { once: true });
  });
}
function micLevelMeter() {                          // drives the orb; analysis only, nothing is sent from here
  const an = actx.createAnalyser(); an.fftSize = 512;
  actx.createMediaStreamSource(new MediaStream([micTrack()])).connect(an);
  const buf = new Float32Array(an.fftSize);
  (function tick() {
    an.getFloatTimeDomainData(buf); let sum = 0; for (const v of buf) sum += v * v;
    $("orb").style.setProperty("--level", micOpen() ? Math.min(1, Math.sqrt(sum / buf.length) * 6).toFixed(2) : 0);
    requestAnimationFrame(tick);
  })();
}

async function startVoice() {
  if (pc) { try { pc.close(); } catch (e) {} }
  const me = (pc = new RTCPeerConnection());
  me.ontrack = (e) => { agentAudio.srcObject = e.streams[0]; ensureSoundOn(); };
  me.addTrack(micTrack(), stream);
  const dc = me.createDataChannel("oai-events");     // direct engine: used to trigger the greeting (pipecat greets server-side)
  let engine = "pipecat";
  dc.onopen = () => { if (engine === "direct" && !greeted) { greeted = true; dc.send(JSON.stringify({ type: "response.create" })); } };
  me.onconnectionstatechange = () => {
    if (me !== pc) return;
    const st = me.connectionState;
    if (st === "connected") { voiceRetry = 0; setPill("Voice connected", "ok"); }
    else if (st === "failed") retryVoice("Voice connection failed");
    else if (st === "disconnected") setTimeout(() => me === pc && me.connectionState === "disconnected" && retryVoice("Voice dropped"), 4000);
  };
  await me.setLocalDescription(await me.createOffer());
  const r = await api(`/api/sweeps/${sweepId}/voice`, { method: "POST", body: me.localDescription.sdp, headers: { "Content-Type": "application/sdp" } });
  if (!r.ok) { const e = new Error((await r.json().catch(() => ({}))).detail || "server said " + r.status); e.fatal = r.status === 503; throw e; }
  engine = r.headers.get("X-Voice-Engine") || "pipecat";
  await me.setRemoteDescription({ type: "answer", sdp: await r.text() });
  if (engine === "direct" && dc.readyState === "open" && !greeted) { greeted = true; dc.send(JSON.stringify({ type: "response.create" })); }
}
function retryVoice(why) {
  if (voiceRetry >= 5) { setPill("Voice unavailable", "bad"); addSystemLine(why + ". Stopped retrying; capture still works."); return; }
  voiceRetry++; setPill("Voice reconnecting…", "warn");
  setTimeout(() => startVoice().catch(voiceFailed), 1500 * voiceRetry);
}
function voiceFailed(e) {
  addSystemLine(e.message); setAgent("off");
  if (e.fatal) setPill("Voice off", "warn"); else retryVoice(e.message);
}

// Mic button: hands-free = Mute/Unmute toggle; noisy room = hold to talk (mic is off unless pressed).
function applyMic() {
  micTrack() && (micTrack().enabled = micOpen());  // disabled track = WebRTC sends silence
  micBtn.setAttribute("aria-pressed", talkMode === "open" ? muted : holding);
  micBtn.textContent = talkMode === "ptt" ? (holding ? "🎙 Talking…" : "🎙 Hold to talk") : (muted ? "🔇 Unmute" : "🎙 Mute");
  micBtn.classList.toggle("ptt", talkMode === "ptt");
  $("talkMode").textContent = talkMode === "ptt" ? "Switch back to hands-free" : "Noisy room? Use hold-to-talk";
  setAgent(agentState);
}
micBtn.onclick = () => { if (talkMode === "open") { muted = !muted; applyMic(); } };
const hold = (on) => (e) => { if (talkMode !== "ptt") return; e.preventDefault(); if (holding !== on) { holding = on; applyMic(); } };
micBtn.addEventListener("pointerdown", hold(true));
["pointerup", "pointerleave", "pointercancel"].forEach((ev) => micBtn.addEventListener(ev, hold(false)));
$("talkMode").onclick = () => { talkMode = talkMode === "open" ? "ptt" : "open"; holding = false; muted = false; applyMic(); };

// ---------------------------------------------------------------- websocket
function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/live?sweep=${sweepId}&t=${encodeURIComponent(TOKEN)}`);
  ws.onopen = () => { if (!pc || pc.connectionState !== "connected") setPill("Connected", "ok"); pump(); };
  ws.onclose = () => { if (finished && !sweepId) return; setPill("Reconnecting…", "warn"); setTimeout(connectWs, 1500); };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    switch (m.type) {
      case "transcript": caption(m); break;
      case "agent_state": setAgent(m.state); break;
      case "inventory": renderInventory(m); break;
      case "coach": showCoach(m.ask || m.text); addSystemLine("Tip: " + (m.ask || m.text)); break;
      case "status": addSystemLine(m.text); if (/disabled|reconnect|fail/i.test(m.text)) setPill(m.text.slice(0, 40), "warn"); break;
      case "packet_ready": showDone(m); break;
    }
  };
}

function showDone(m) {
  finished = true; stream?.getVideoTracks().forEach((t) => t.stop());
  $("live").hidden = true; $("done").hidden = false; $("summary").textContent = m.summary;
  $("reportLink").href = m.report + "?t=" + encodeURIComponent(TOKEN); setPill("Packet ready", "ok");
}

// ---------------------------------------------------------------- start / finish
function fail(msg) { $("err").textContent = msg; $("start").disabled = false; setPill("Not started", "bad"); }

async function start() {
  unlockAudio();                                   // first line: still inside the user's tap
  $("start").disabled = true; $("err").textContent = "";
  if (!TOKEN) return fail("This link is missing its access token. Scan the QR code from the server again.");
  if (!navigator.mediaDevices?.getUserMedia) return fail("Camera needs a secure (https) link. Use the https address the server printed.");
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "environment", width: { ideal: CONFIG.video.width }, height: { ideal: CONFIG.video.height }, frameRate: { ideal: 30, max: 30 } }, audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  } catch (e) { return fail(e.name === "NotAllowedError" ? "Camera or microphone permission was denied. Allow both in your browser settings, then try again." : "Could not open camera/mic: " + e.name); }
  video.srcObject = stream; await video.play();
  const settings = stream.getVideoTracks()[0].getSettings();
  const r = await api("/api/sweeps", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ country: $("country").value.trim().toUpperCase(), currency: $("currency").value.trim().toUpperCase(),
      device: { ...settings, orientation: screen.orientation && screen.orientation.type, ua: navigator.userAgent } }) });
  if (!r.ok) return fail(r.status === 401 ? "Wrong or expired access token. Scan the QR code again." : "Server said " + r.status);
  sweepId = (await r.json()).sweep_id; t0 = performance.now();
  $("welcome").hidden = true; $("live").hidden = false; setAgent("connecting"); setPill("Connecting…", "warn");
  setInterval(() => { const s = Math.floor((performance.now() - t0) / 1000); $("timer").textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; }, 1000);
  try { if (screen.orientation && screen.orientation.lock) await screen.orientation.lock(screen.orientation.type); } catch (e) {}
  try {
    wakeLock = await navigator.wakeLock.request("screen");
    document.addEventListener("visibilitychange", async () => { if (document.visibilityState === "visible" && sweepId && !finished) wakeLock = await navigator.wakeLock.request("screen").catch(() => null); });
  } catch (e) { showCoach("Please keep the screen on during the sweep."); }
  connectWs(); micLevelMeter(); applyMic(); scheduleKeyframes();
  startVoice().catch(voiceFailed);
}

let confirmTimer;
async function finish() {
  const b = $("finish");
  if (!b.classList.contains("confirm")) {          // first tap arms it; second tap within 4 s confirms
    b.classList.add("confirm"); b.textContent = "Tap again to confirm";
    confirmTimer = setTimeout(() => { b.classList.remove("confirm"); b.innerHTML = "Finish &amp; build packet"; }, 4000);
    return;
  }
  clearTimeout(confirmTimer); b.disabled = true; b.textContent = "Building your packet…"; setPill("Building packet…", "warn");
  finished = true;                                  // stop taking keyframes
  while (queue.length || sending) await new Promise((r) => setTimeout(r, 300));   // flush what is already captured
  await api(`/api/sweeps/${sweepId}/finish`, { method: "POST" });
}
$("start").onclick = start; $("finish").onclick = finish;

// ---------------------------------------------------------------- room measurement (WebXR, Chrome on Android)
// The phone's AR tracking (ARCore, through the browser; nothing to install) gives points in metres. The user aims
// the crosshair at each floor corner in order around the room, then at a point where a wall meets the ceiling.
// Chrome needs the camera for AR, so the page's camera feed is stopped while measuring and restarted afterwards;
// the voice call (microphone) keeps running.
let xr = null;          // { session, refSpace, hitSource, gl, corners: [[x,y,z]], ceilingY, stage: 1 | 2, hit }

async function xrSupported() {
  try { return !!(navigator.xr && await navigator.xr.isSessionSupported("immersive-ar")); } catch (e) { return false; }
}
xrSupported().then((ok) => { $("measureRoom").hidden = !ok; });

function xrText() {
  const n = xr.corners.length;
  $("xrText").textContent = xr.stage === 1
    ? `Aim the circle at a floor corner and tap "Add point". Go around the room in order (${n} added${n < 3 ? ", need at least 3" : ""}). Then "Next".`
    : (xr.ceilingY == null ? "Now aim at a point where a wall meets the ceiling and tap \"Add point\" (for the height), or tap \"Skip\"."
                           : "Height point added. Tap \"Done\".");
  $("xrNext").textContent = xr.stage === 1 ? "Next" : (xr.ceilingY == null ? "Skip" : "Done");
  $("xrNext").disabled = xr.stage === 1 && n < 3;
}

async function startRoomMeasure() {
  if (!sweepId || xr) return;
  let session;
  try {
    session = await navigator.xr.requestSession("immersive-ar", {
      requiredFeatures: ["hit-test"], optionalFeatures: ["dom-overlay", "local-floor"], domOverlay: { root: $("xrOverlay") } });
  } catch (e) { addSystemLine("Room measuring could not start: " + (e.message || e.name)); return; }
  stream?.getVideoTracks().forEach((t) => { t.stop(); stream.removeTrack(t); });   // AR needs the camera
  const gl = document.createElement("canvas").getContext("webgl", { xrCompatible: true });
  await session.updateRenderState({ baseLayer: new XRWebGLLayer(session, gl) });
  let refSpace;
  try { refSpace = await session.requestReferenceSpace("local-floor"); } catch (e) { refSpace = await session.requestReferenceSpace("local"); }
  const hitSource = await session.requestHitTestSource({ space: await session.requestReferenceSpace("viewer") });
  xr = { session, refSpace, hitSource, gl, corners: [], ceilingY: null, stage: 1, hit: null };
  $("xrOverlay").hidden = false; xrText();
  session.addEventListener("select", addXrPoint);            // tapping the AR view also adds a point
  session.addEventListener("end", onXrEnd);
  session.requestAnimationFrame(onXrFrame);
}

function onXrFrame(time, frame) {
  if (!xr) return;
  frame.session.requestAnimationFrame(onXrFrame);
  const layer = frame.session.renderState.baseLayer;           // nothing to draw: the browser shows the camera
  xr.gl.bindFramebuffer(xr.gl.FRAMEBUFFER, layer.framebuffer);
  xr.gl.clearColor(0, 0, 0, 0); xr.gl.clear(xr.gl.COLOR_BUFFER_BIT);
  const hits = frame.getHitTestResults(xr.hitSource);
  const pose = hits.length ? hits[0].getPose(xr.refSpace) : null;
  xr.hit = pose ? [pose.transform.position.x, pose.transform.position.y, pose.transform.position.z] : null;
  $("xrReticle").classList.toggle("ok", !!xr.hit);
}

function addXrPoint() {
  if (!xr) return;
  if (!xr.hit) { $("xrText").textContent = "No surface under the circle yet. Move the phone slowly over the floor or wall, then try again."; return; }
  if (xr.stage === 1) xr.corners.push(xr.hit.map((v) => +v.toFixed(4)));
  else xr.ceilingY = +xr.hit[1].toFixed(4);
  xrText();
}

async function finishRoomMeasure() {
  const body = JSON.stringify({ corners: xr.corners, ceiling_y: xr.ceilingY });
  const id = sweepId, session = xr.session;
  try {
    const r = await api(`/api/sweeps/${id}/room/corners`, { method: "POST", headers: { "Content-Type": "application/json" }, body });
    if (!r.ok) addSystemLine("Room measurement was not saved (server said " + r.status + ").");
  } catch (e) { addSystemLine("Room measurement was not saved: " + e.message); }
  session.end();                                              // the server's result arrives as a status line
}

async function onXrEnd() {
  try { xr?.hitSource.cancel(); } catch (e) {}
  xr = null; $("xrOverlay").hidden = true;
  if (finished) return;
  try {                                                       // give the camera back to the sweep
    const v = await navigator.mediaDevices.getUserMedia({ video: {
      facingMode: "environment", width: { ideal: CONFIG.video.width }, height: { ideal: CONFIG.video.height }, frameRate: { ideal: 30, max: 30 } } });
    stream.addTrack(v.getVideoTracks()[0]); video.srcObject = stream; await video.play();
  } catch (e) { addSystemLine("The camera could not restart after measuring: " + e.name); }
}

// Buttons on the overlay must not also count as taps on the AR view.
$("xrOverlay").querySelector(".xrpanel").addEventListener("beforexrselect", (e) => e.preventDefault());
$("measureRoom").onclick = startRoomMeasure;
$("xrAdd").onclick = addXrPoint;
$("xrUndo").onclick = () => {
  if (!xr) return;
  if (xr.stage === 2) { if (xr.ceilingY != null) xr.ceilingY = null; else xr.stage = 1; }   // back to the corners step
  else xr.corners.pop();
  xrText();
};
$("xrNext").onclick = () => { if (!xr) return; if (xr.stage === 1) { xr.stage = 2; xrText(); } else finishRoomMeasure(); };
$("xrCancel").onclick = () => xr?.session.end();
if (!TOKEN) $("err").textContent = "Open this page from the QR code or link the server printed (it includes your access token).";
