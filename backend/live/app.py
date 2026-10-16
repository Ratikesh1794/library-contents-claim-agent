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
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from backend import deps, pipeline
from backend.core import store
from backend.live import agent
from backend.stages import dedupe, identify, track
from backend.live.coach import Coach
from backend.config import CFG, ROOT, Config
from backend.schemas import Frame, Sighting, UserFact

log = logging.getLogger("live")
WEB = ROOT / "web"
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
    down: asyncio.Queue = field(default_factory=asyncio.Queue)     # agent/app -> browser
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
               banner: bool = False, voice=agent) -> FastAPI:
    """`voice` = module/object with start_call, run, hangup (backend.agent); tests inject a fake."""
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

    # ------------------------------------------------------------------ auth
    def check_token(t: str | None) -> bool:
        return bool(t) and hmac.compare_digest(t, app.state.token)

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
            app.state.sessions[sweep_id] = s
        return s

    async def frame_worker(s: SweepSession):
        """Identify keyframes as they arrive; update the panel and let the Coach react."""
        d = app.state.deps
        vision, cat = d["vision"] or deps.vision(), d["catalogue"] or deps.catalogue()
        prev = None                                  # (frame, sightings) of the last frame: lets identify reuse reads
        while True:
            frame: Frame = await s.frame_q.get()
            try:
                sightings = await asyncio.to_thread(identify.identify_frame, s.sd, frame, vision, cat, cfg, prev)
                store.write(s.sd, f"sightings/{frame.id}.json", sightings)
                prev = (frame, sightings)
            except Exception as e:
                log.warning("identify %s failed: %s", frame.id, e)
                continue
            await push_inventory(s)
            for m in s.coach.update(sightings, s.now(), s.last_user_speech_t):
                await s.down.put({"type": "coach", **m})
                if m["type"] == "CAPTURE_ISSUE":
                    await s.inject.put(f"CAPTURE_ISSUE region={m['region']} reason={m['reason']} ask=\"{m['ask']}\"")
                else:
                    await s.inject.put(f"STATUS {m['text']}")

    def current_objects(sd: Path):
        """Same dedupe as the pipeline (tracking included), on the frames identified so far."""
        frames = [f for f in store.read(sd, "frames.json", list[Frame], default=[])
                  if (sd / "sightings" / f"{f.id}.json").exists()]
        sightings = []
        for f in frames:
            sightings += store.read(sd, f"sightings/{f.id}.json", list[Sighting], default=[])
        return dedupe.build_objects(sightings, frames, track.links(sd, frames, cfg), cfg)

    async def push_inventory(s: SweepSession):
        objs = await asyncio.to_thread(current_objects, s.sd)    # image alignment is CPU work: off the event loop
        books = [o for o in objs if o.kind == "book"]
        await s.down.put({"type": "inventory",
                          "books": [{"id": o.id, "title": o.title, "status": o.status, "reason": o.reason} for o in books],
                          "items": [{"id": o.id, "category": o.category} for o in objs if o.kind == "item"],
                          "counts": {"books": len(books), "identified": sum(o.status == "identified" for o in books)}})

    async def finish(s: SweepSession):
        if s.finished:
            return
        s.finished = True
        await s.down.put({"type": "status", "text": "Building your claim packet…"})
        try:
            packet = await asyncio.to_thread(pipeline.run, s.sd, None, None, cfg, pipeline_deps or {})
        except Exception as e:
            log.exception("pipeline failed")
            s.finished = False
            await s.down.put({"type": "status", "text": f"Pipeline failed: {e}"})
            return
        store.write(s.sd, "transcript.json", s.transcript)      # what was said, for the audit trail
        summary = (s.sd / "voice_summary.txt").read_text()
        await s.down.put({"type": "packet_ready", "summary": summary, "report": f"/api/sweeps/{s.sweep_id}/report"})
        await s.inject.put(f"PACKET_READY summary_facts=\"{summary}\"")

    # ------------------------------------------------------------------ routes
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        if not check_token(request.query_params.get("t")):    # stale QR / restarted server: say so plainly
            return HTMLResponse(EXPIRED_PAGE, status_code=401)
        return (WEB / "index.html").read_text()

    @app.post("/api/sweeps", dependencies=[Depends(require_token)])
    async def create_sweep(request: Request):
        body = await request.json() if await request.body() else {}
        sd = store.new_sweep(body.get("country"), body.get("currency") or "USD", cfg.sweeps_dir, device=body.get("device"))
        session_for(sd.name)
        return {"sweep_id": sd.name}

    @app.post("/api/sweeps/{sweep_id}/frames", dependencies=[Depends(require_token)])
    async def upload_frame(sweep_id: str, request: Request, seq: int, t: float, w: int = 0, h: int = 0,
                           blur: float = 0, glare: float = 0, dark: float = 0):
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

    @app.post("/api/sweeps/{sweep_id}/voice", dependencies=[Depends(require_token)])
    async def start_voice(sweep_id: str, request: Request):
        """WebRTC signalling: SDP offer in, SDP answer out. One call per sweep; a new offer replaces the old call."""
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
            answer, s.call_id = await voice.start_call(offer, store.read(s.sd, "meta.json"), cfg)
        except Exception as e:
            log.warning("voice call failed: %s", e)
            raise HTTPException(502, f"Could not start the voice call: {e}")
        s.voice_task = asyncio.create_task(voice.run(s, s.call_id, cfg))
        return Response(answer, media_type="application/sdp")

    @app.post("/api/sweeps/{sweep_id}/finish", dependencies=[Depends(require_token)])
    async def finish_route(sweep_id: str):
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

        async def reader():                               # nothing to read (voice is WebRTC); detects disconnects
            while True:
                await ws.receive_text()

        async def writer():
            while True:
                await ws.send_text(json.dumps(await s.down.get()))

        try:
            await push_inventory(s)
            await asyncio.gather(reader(), writer())
        except Exception:
            pass

    app.mount("/static", StaticFiles(directory=WEB), name="static")
    return app


app = create_app(banner=True)
