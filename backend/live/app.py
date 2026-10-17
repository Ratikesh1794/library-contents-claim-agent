"""FastAPI app: serves the phone web app, the sweep API, and the /ws/live bridge.

Run:  uvicorn backend.live.app:app --host 0.0.0.0 --port 8000 --ws-max-size 4000000   (or scripts/run.sh)
Auth: every API call and the WebSocket need the sweep token (?t= or X-Sweep-Token). The page itself
needs ?t= too. Origin of WS handshakes must be same-host or in allowed origins / PUBLIC_ORIGIN.

Flow per sweep:  POST /api/sweeps -> phone uploads keyframes (POST .../frames) -> a worker runs
identify_frame on each -> inventory + Coach messages go down the WebSocket -> finish -> pipeline.
Voice: the phone POSTs its WebRTC offer to /api/sweeps/<id>/voice; we create the OpenAI call and attach
to it sideband (backend/live/agent.py). Voice audio never passes through this server.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import os
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import cv2
import qrcode
import qrcode.image.svg
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from backend import deps, pipeline
from backend.core import store
from backend.live import agent, voice_pipecat
from backend.stages import dedupe, identify, track
from backend.stages.room_corners import room_from_corners
from backend.live.coach import Coach
from backend.config import CFG, ROOT, Config
from backend.schemas import Frame, Sighting, UserFact

log = logging.getLogger("live")
WEB = ROOT / "web"
HISTORY_TYPES = {"transcript", "coach", "status", "frame", "agent_state", "packet_ready", "phone", "room"}
EXPIRED_PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<body style="font:17px system-ui;background:#0f1115;color:#eee;display:grid;place-items:center;min-height:90vh;text-align:center;padding:24px">
<div><h2>This link has expired</h2><p style="color:#9aa3b2">The server was restarted or the link is incomplete.<br>
Scan the QR code shown in the server's terminal again.</p></div>"""


@dataclass
class SweepSession:
    sweep_id: str
    sd: Path
    cfg: Config
    coach: Coach
    t0: float = field(default_factory=time.time)
    down: asyncio.Queue = field(default_factory=asyncio.Queue)     # agent/app -> every connected screen (fan-out)
    subs: set = field(default_factory=set)                          # one queue per open WebSocket (phone, dashboards)
    history: deque = field(default_factory=lambda: deque(maxlen=800))   # replayed to a dashboard that joins late
    phones: int = 0                                                 # phones currently connected
    inject: asyncio.Queue = field(default_factory=asyncio.Queue)   # app-state text -> agent
    frame_q: asyncio.Queue = field(default_factory=asyncio.Queue)
    transcript: list = field(default_factory=list)
    last_user_speech_t: float = float("-inf")
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    hooks: object = None
    tasks: list = field(default_factory=list)
    finished: bool = False
    call_id: str | None = None                                      # current OpenAI WebRTC call
    voice_task: asyncio.Task | None = None                          # sideband controller for that call

    def now(self) -> float:
        return time.time() - self.t0


class RateLimit:
    """Sliding-window limiter: at most `n` hits per `per` seconds for each key. Over the limit => HTTP 429."""

    def __init__(self):
        self.hits: dict[tuple, deque] = defaultdict(deque)

    def check(self, key: tuple, limit: tuple[int, float]) -> None:
        n, per = limit
        now, q = time.monotonic(), self.hits[key]
        while q and now - q[0] >= per:
            q.popleft()
        if len(q) >= n:
            raise HTTPException(429, "Too many requests, slow down")
        q.append(now)


class Hooks:
    """What the voice agent's tools do to the app. Agent tools write USER FACTS only."""
    def __init__(self, s: SweepSession, finish):
        self.s, self._finish = s, finish

    def on_fact(self, ref, text, turn_id):
        facts = store.read(self.s.sd, "user_facts.json", list[UserFact], default=[])
        store.write(self.s.sd, "user_facts.json", facts + [UserFact(ref=ref, text=text, turn_id=turn_id, t=self.s.now())])

    def on_skip(self, region, action):
        if action == "skip":
            self.s.coach.close(region)
            self.on_fact(region, "skip", None)

    async def on_finish(self):
        await self._finish(self.s)


def create_app(token: str | None = None, cfg: Config = CFG, vision=None, catalogue=None, pipeline_deps=None,
               banner: bool = False, voice=None) -> FastAPI:
    """`voice` = module/object with start_call, run, hangup. Default from VOICE_ENGINE: backend.live.voice_pipecat
    ("pipecat") or backend.live.agent ("direct"). Tests inject a fake."""
    if voice is None:
        voice = agent if cfg.voice_engine == "direct" else voice_pipecat
    @asynccontextmanager
    async def lifespan(app):
        if banner:
            print(f"\nSweep token: {app.state.token}\nOpen on the phone: <your https url>/?t={app.state.token}\n")
        yield

    app = FastAPI(title="Library Contents Claim Agent", lifespan=lifespan)
    app.state.token = token or os.environ.get("SWEEP_TOKEN") or secrets.token_urlsafe(12)
    app.state.sessions = {}
    app.state.deps = {"vision": vision, "catalogue": catalogue}
    app.state.voice = voice
    limits = RateLimit()

    # ------------------------------------------------------------------ auth
    def check_token(t: str | None) -> bool:
        if not t:
            return False
        return hmac.compare_digest(t, app.state.token)

    def require_token(request: Request):
        if not check_token(request.headers.get("x-sweep-token") or request.query_params.get("t")):
            raise HTTPException(401, "missing or wrong token")

    def origin_ok(origin: str | None, host: str | None) -> bool:
        if not origin:
            return True                                        # non-browser client
        allowed = {urlparse(o).netloc for o in (*cfg.allowed_origins, cfg.public_origin) if o}
        return urlparse(origin).netloc in allowed | {host}

    def get_dir(sweep_id: str) -> Path:
        try:
            return store.sweep_path(sweep_id, cfg.sweeps_dir)
        except (ValueError, FileNotFoundError):
            raise HTTPException(404, "no such sweep")

    # ------------------------------------------------------------------ session / workers
    def session_for(sweep_id: str) -> SweepSession:
        s = app.state.sessions.get(sweep_id)
        if s is None:
            s = SweepSession(sweep_id, get_dir(sweep_id), cfg, Coach(cfg))
            s.hooks = Hooks(s, finish)
            s.tasks.append(asyncio.create_task(frame_worker(s)))
            s.tasks.append(asyncio.create_task(fanout(s)))
            app.state.sessions[sweep_id] = s
        return s

    async def fanout(s: SweepSession):
        """Copy every live message to every connected screen; keep the meaningful ones for late joiners."""
        while True:
            m = await s.down.get()
            if m.get("type") in HISTORY_TYPES and not (m["type"] == "transcript" and m.get("mode") == "append"):
                s.history.append(m)
            for q in list(s.subs):
                try:
                    q.put_nowait(m)
                except asyncio.QueueFull:
                    log.warning("a screen is not keeping up; dropped a %s message", m.get("type"))

    async def frame_worker(s: SweepSession):
        """Identify keyframes as they arrive; update the panel and let the Coach react."""
        d = app.state.deps
        vision, cat = d["vision"] or deps.vision(), d["catalogue"] or deps.catalogue()
        anchor = None                                # (frame, sightings) of the last ANALYSED frame
        while True:
            frame: Frame = await s.frame_q.get()
            skipped = 0
            while not s.frame_q.empty():             # stay current: work on the newest frame; the ones skipped
                frame = s.frame_q.get_nowait()       # here are identified at finish (identify.run)
                skipped += 1
            if skipped:
                log.info("live: skipped %d stale frame(s), now on %s", skipped, frame.id)
            started = time.time()
            try:
                sightings = await asyncio.to_thread(identify.carry_over, s.sd, frame, anchor, cfg)
                if sightings is None:                # something new in view: analyse it
                    sightings = await asyncio.to_thread(identify.identify_frame, s.sd, frame, vision, cat, cfg, anchor)
                    anchor = (frame, sightings)
                store.write(s.sd, f"sightings/{frame.id}.json", sightings)
            except Exception as e:
                log.warning("identify %s failed: %s", frame.id, e)
                await s.down.put({"type": "frame", "frame_id": frame.id, "t": frame.t, "error": str(e)[:200]})
                continue
            books = [x for x in sightings if x.kind == "book"]
            await s.down.put({"type": "frame", "frame_id": frame.id, "t": frame.t, "skipped": skipped,
                              "carried": identify.is_carried(sightings), "ms": int((time.time() - started) * 1000),
                              "books": len(books), "identified": sum(1 for x in books if x.accepted),
                              "items": sum(1 for x in sightings if x.kind == "item"),
                              "titles": [x.accepted.title for x in books if x.accepted][:6]})
            await push_inventory(s)
            for m in s.coach.update(sightings, s.now(), s.last_user_speech_t):
                await s.down.put({"type": "coach", **m})
                if m["type"] == "CAPTURE_ISSUE":
                    await s.inject.put(f"CAPTURE_ISSUE region={m['region']} reason={m['reason']} ask=\"{m['ask']}\"")
                else:
                    await s.inject.put(f"STATUS {m['text']}")

    def current_objects(sd: Path):
        """Same dedupe as the pipeline (tracking included), on the frames identified so far.
        -> (objects, frames uploaded, frames identified)."""
        all_frames = store.read(sd, "frames.json", list[Frame], default=[])
        frames = [f for f in all_frames if (sd / "sightings" / f"{f.id}.json").exists()]
        sightings = []
        for f in frames:
            sightings += store.read(sd, f"sightings/{f.id}.json", list[Sighting], default=[])
        return dedupe.build_objects(sightings, frames, track.links(sd, frames, cfg), cfg), len(all_frames), len(frames)

    async def push_inventory(s: SweepSession):
        objs, n_frames, n_done = await asyncio.to_thread(current_objects, s.sd)   # CPU work: off the event loop
        books = [o for o in objs if o.kind == "book"]
        common = lambda o: {"id": o.id, "status": o.status, "reason": o.reason, "frame": Path(o.frame_ref).name,
                            "bbox": o.bbox, "seen": len(o.sighting_ids)}
        await s.down.put({"type": "inventory",
                          "books": [{**common(o), "title": o.title, "author": o.author, "edition": o.edition,
                                     "shelf": o.shelf, "confidence": o.id_confidence} for o in books],
                          "items": [{**common(o), "category": o.category, "material": o.material,
                                     "brand_model": o.brand_model} for o in objs if o.kind == "item"],
                          "counts": {"books": len(books), "identified": sum(o.status == "identified" for o in books),
                                     "frames": n_frames, "analysed": n_done}})

    async def finish(s: SweepSession):
        if s.finished:
            return
        s.finished = True
        await s.down.put({"type": "status", "text": "Building your claim packet…"})
        try:
            injected = {k: v for k, v in app.state.deps.items() if k in ("vision", "catalogue") and v is not None}
            packet = await asyncio.to_thread(pipeline.run, s.sd, None, None, cfg, {**injected, **(pipeline_deps or {})})
        except Exception as e:
            log.exception("pipeline failed")
            s.finished = False
            await s.down.put({"type": "status", "text": f"Pipeline failed: {e}"})
            return
        store.write(s.sd, "transcript.json", s.transcript)      # what was said, for the audit trail
        summary = (s.sd / "voice_summary.txt").read_text()
        await s.down.put({"type": "packet_ready", "summary": summary, "report": f"/api/sweeps/{s.sweep_id}/report",
                          "packet": f"/api/sweeps/{s.sweep_id}/packet",
                          "totals": packet.totals.model_dump() if packet else None,
                          "review": len(packet.review_queue) if packet else None,
                          "room": packet.room.model_dump() if packet and packet.room else None})
        await s.inject.put(f"PACKET_READY summary_facts=\"{summary}\"")

    # ------------------------------------------------------------------ routes
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        if not check_token(request.query_params.get("t")):    # stale QR / restarted server: say so plainly
            return HTMLResponse(EXPIRED_PAGE, status_code=401)
        return (WEB / "index.html").read_text()

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request):
        """Laptop screen: records organised + everything happening live (open it next to the server)."""
        if not check_token(request.query_params.get("t")):
            return HTMLResponse(EXPIRED_PAGE, status_code=401)
        return (WEB / "dashboard.html").read_text()

    @app.get("/api/connect", dependencies=[Depends(require_token)])
    def connect_info(request: Request):
        """The phone's link and its QR code (SVG) for the dashboard. Uses the public HTTPS origin from run.sh."""
        origin = (cfg.public_origin or str(request.base_url)).rstrip("/")
        url = f"{origin}/?t={app.state.token}"
        svg = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, border=2).to_string().decode()
        return {"phone_url": url, "qr_svg": svg, "https": url.startswith("https://")}

    @app.get("/api/sweeps", dependencies=[Depends(require_token)])
    def list_sweeps():
        """Recent sweeps, newest first, for the dashboard's picker."""
        out = []
        root = Path(cfg.sweeps_dir)
        for d in sorted(root.glob("sw_*"), reverse=True)[:50] if root.exists() else []:
            meta = store.read(d, "meta.json", dict, default=None)
            if not meta:
                continue
            live = app.state.sessions.get(d.name)
            out.append({"id": d.name, "created_at": meta.get("created_at"), "country": meta.get("country"),
                        "currency": meta.get("currency"), "frames": len(store.read(d, "frames.json", list, default=[])),
                        "packet": (d / "claim_packet.json").exists(),
                        "live": bool(live and live.phones > 0 and not live.finished)})
        return out

    @app.get("/api/sweeps/{sweep_id}/crop", dependencies=[Depends(require_token)])
    def crop(sweep_id: str, f: str, b: str, w: int = 160):
        """Thumbnail of one record: frame `f`, box `b` = "x0,y0,x1,y1", long side `w` px."""
        if not store.FRAME_NAME_RE.match(f):
            raise HTTPException(404)
        try:
            x0, y0, x1, y1 = (int(v) for v in b.split(","))
        except ValueError:
            raise HTTPException(400, "b must be x0,y0,x1,y1")
        img = cv2.imread(str(get_dir(sweep_id) / "frames" / f))
        if img is None:
            raise HTTPException(404)
        H, W = img.shape[:2]
        mx, my = (x1 - x0) // 12, (y1 - y0) // 12
        c = img[max(0, y0 - my):min(H, y1 + my), max(0, x0 - mx):min(W, x1 + mx)]
        if not c.size:
            raise HTTPException(400, "empty box")
        k = min(1.0, max(40, min(w, 400)) / max(c.shape[:2]))
        if k < 1:
            c = cv2.resize(c, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", c, [cv2.IMWRITE_JPEG_QUALITY, 82])
        return Response(buf.tobytes(), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    @app.post("/api/sweeps", dependencies=[Depends(require_token)])
    async def create_sweep(request: Request):
        limits.check(("new_sweep",), cfg.rate_new_sweep)
        body = await request.json() if await request.body() else {}
        sd = store.new_sweep(body.get("country"), body.get("currency") or "USD", cfg.sweeps_dir, device=body.get("device"))
        session_for(sd.name)
        return {"sweep_id": sd.name}

    @app.post("/api/sweeps/{sweep_id}/frames", dependencies=[Depends(require_token)])
    async def upload_frame(sweep_id: str, request: Request, seq: int, t: float, w: int = 0, h: int = 0,
                           blur: float = 0, glare: float = 0, dark: float = 0):
        limits.check((sweep_id, "frames"), cfg.rate_frames)
        if int(request.headers.get("content-length") or 0) > cfg.keyframe_max_bytes:
            raise HTTPException(413, "keyframe too large")
        body = await request.body()
        if len(body) > cfg.keyframe_max_bytes:
            raise HTTPException(413, "keyframe too large")
        s = session_for(sweep_id)
        async with s.lock:
            frames = store.read(s.sd, "frames.json", list[Frame], default=[])
            if any(f.seq == seq for f in frames):                   # re-upload after reconnect
                return {"ok": True, "duplicate": True}
            fid = f"f_{seq:04d}"
            (s.sd / "frames" / f"{fid}.jpg").write_bytes(body)
            f = Frame(id=fid, path=f"frames/{fid}.jpg", t=t, w=w, h=h, blur=blur, glare=glare, dark=dark, seq=seq)
            store.write(s.sd, "frames.json", frames + [f])
        await s.frame_q.put(f)
        return {"ok": True, "frame_id": fid}

    @app.post("/api/sweeps/{sweep_id}/room/corners", dependencies=[Depends(require_token)])
    async def room_corners(sweep_id: str, request: Request):
        """Floor corners (and optional wall/ceiling point) measured with WebXR in the browser: metres, y up."""
        limits.check((sweep_id, "room"), cfg.rate_finish)
        try:
            data = await request.json()
            corners = [[float(v) for v in c] for c in data["corners"]]
            ceiling = data.get("ceiling_y")
            ceiling = None if ceiling is None else float(ceiling)
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, "expected {corners: [[x, y, z], ...], ceiling_y: number|null}")
        if not 3 <= len(corners) <= 50 or any(len(c) != 3 or not all(map(math.isfinite, c)) for c in corners):
            raise HTTPException(400, "need 3-50 corners of 3 finite numbers")
        s = session_for(sweep_id)
        store.write(s.sd, "room_corners.json", {"corners": corners, "ceiling_y": ceiling})
        room = room_from_corners(corners, ceiling)
        text = f"Room measured: floor {room.floor_area_m2} m² ({room.length_m} × {room.width_m} m)" + \
               (f", height {room.height_m} m" if room.height_m else "")
        await s.down.put({"type": "status", "text": text})
        await s.down.put({"type": "room", **room.model_dump()})
        await s.inject.put(f"STATUS {text}")
        return room.model_dump()

    @app.post("/api/sweeps/{sweep_id}/voice", dependencies=[Depends(require_token)])
    async def start_voice(sweep_id: str, request: Request):
        """WebRTC signalling: SDP offer in, SDP answer out. One call per sweep; a new offer replaces the old call."""
        limits.check((sweep_id, "voice"), cfg.rate_voice)
        if not os.environ.get("OPENAI_API_KEY"):
            raise HTTPException(503, "Voice agent disabled: OPENAI_API_KEY not set")
        offer = (await request.body()).decode(errors="replace")
        if not offer.startswith("v=") or len(offer) > 50_000:
            raise HTTPException(400, "expected an SDP offer")
        s = session_for(sweep_id)
        if s.voice_task:
            s.voice_task.cancel()
        if s.call_id:
            asyncio.create_task(voice.hangup(s.call_id))
        try:
            answer, call_id = await voice.start_call(offer, store.read(s.sd, "meta.json"), cfg)
        except Exception as e:
            log.warning("voice call failed: %s", e)
            raise HTTPException(502, f"Could not start the voice call: {e}")
        s.call_id = call_id
        s.voice_task = asyncio.create_task(voice.run(s, call_id, cfg))
        engine = "direct" if voice is agent else "pipecat" if voice is voice_pipecat else "custom"
        return Response(answer, media_type="application/sdp", headers={"X-Voice-Engine": engine})

    @app.post("/api/sweeps/{sweep_id}/finish", dependencies=[Depends(require_token)])
    async def finish_route(sweep_id: str):
        limits.check((sweep_id, "finish"), cfg.rate_finish)
        s = session_for(sweep_id)
        asyncio.create_task(finish(s))
        return {"ok": True}

    @app.get("/api/sweeps/{sweep_id}/packet", dependencies=[Depends(require_token)])
    def packet(sweep_id: str):
        p = get_dir(sweep_id) / "claim_packet.json"
        if not p.exists():
            raise HTTPException(404, "packet not built yet")
        return JSONResponse(json.loads(p.read_text()))

    @app.get("/api/sweeps/{sweep_id}/report", dependencies=[Depends(require_token)])
    def report(sweep_id: str):
        p = get_dir(sweep_id) / "report.html"
        if not p.exists():
            raise HTTPException(404, "report not built yet")
        return HTMLResponse(p.read_text())

    @app.get("/api/sweeps/{sweep_id}/frames/{name}", dependencies=[Depends(require_token)])
    def frame_file(sweep_id: str, name: str):
        if not store.FRAME_NAME_RE.match(name):
            raise HTTPException(404)
        p = get_dir(sweep_id) / "frames" / name
        if not p.exists():
            raise HTTPException(404)
        return FileResponse(p)

    @app.websocket("/ws/live")
    async def ws_live(ws: WebSocket):
        if not origin_ok(ws.headers.get("origin"), ws.headers.get("host")) or not check_token(ws.query_params.get("t")):
            await ws.close(code=4401)
            return
        try:
            s = session_for(ws.query_params.get("sweep", ""))
        except HTTPException:
            await ws.close(code=4404)
            return
        await ws.accept()
        role = "dashboard" if ws.query_params.get("role") == "dashboard" else "phone"
        q: asyncio.Queue = asyncio.Queue(maxsize=5000)

        async def reader():                               # nothing to read (voice is WebRTC); detects disconnects
            while True:
                await ws.receive_text()

        async def writer():
            while True:
                await ws.send_text(json.dumps(await q.get()))

        if role == "dashboard":                           # catch up: what happened before this screen opened
            meta = store.read(s.sd, "meta.json", dict, default={})
            await ws.send_text(json.dumps({"type": "hello", "sweep_id": s.sweep_id, "meta": meta,
                                           "phone": s.phones > 0, "finished": s.finished}))
            for m in list(s.history):
                await ws.send_text(json.dumps(m))
        s.subs.add(q)
        if role == "phone":
            s.phones += 1
            await s.down.put({"type": "phone", "connected": True})
        try:
            await push_inventory(s)
            await asyncio.gather(reader(), writer())
        except Exception:
            pass
        finally:
            s.subs.discard(q)
            if role == "phone":
                s.phones -= 1
                await s.down.put({"type": "phone", "connected": s.phones > 0})

    @app.middleware("http")
    async def revalidate_static(request: Request, call_next):
        """Browsers otherwise reuse an old app.js/css after an update; no-cache still uses the ETag (304s)."""
        resp = await call_next(request)
        if request.url.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    app.mount("/static", StaticFiles(directory=WEB), name="static")
    return app


app = create_app(banner=True)
