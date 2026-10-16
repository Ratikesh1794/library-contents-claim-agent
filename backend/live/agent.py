"""Voice agent: OpenAI Realtime over WebRTC, phone <-> OpenAI directly, with this server attached "sideband".

    phone ──WebRTC audio (browser echo cancellation + noise suppression)──► OpenAI Realtime
      │  POST /api/sweeps/<id>/voice  (SDP offer)                               ▲
      ▼                                                                         │ sideband WebSocket (same call)
    live.py ── start_call(): creates the call with OUR session config ──────────┘ run(): tools, coach messages,
               (API key never leaves the laptop)                                    captions + agent state -> UI

Why: the old path (PCM over our WebSocket, played with WebAudio) bypassed the browser's echo cancellation,
so the agent heard itself and the room. Over WebRTC the browser cleans the mic like a video call does.

It talks to the rest of the app through `SweepSession` (live.py):
    session.down     -> browser   transcript {role, item_id, text, mode: append|set} | agent_state {state} | status {text}
    session.inject   app-state text for the agent ("CAPTURE_ISSUE ...", "STATUS ...", "PACKET_READY ...")
    session.hooks    on_fact(ref, text, turn_id), on_skip(region, action), on_finish()
The agent only talks and calls tools (design D2). It has no tool that writes a title, price or measurement.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from backend.config import CFG
from backend.prompts import agent as prompts, fill

log = logging.getLogger(__name__)

def _fn(name, description, **props):
    return {"type": "function", "name": name, "description": description,
            "parameters": {"type": "object", "properties": {k: {"type": "string", "description": v} for k, v in props.items()},
                           "required": list(props)}}


TOOLS = [
    _fn("record_user_fact", "Record something the user told you about an item (e.g. 'first edition', 'signed', 'print').",
        target_hint="what it refers to: item id, shelf row, or title words", fact="what the user said"),
    _fn("mark_region", "Skip or include a shelf region on the user's instruction.",
        region="region id, e.g. row2", action="skip or include"),
    _fn("finish_sweep", "Call when the user says they are done walking the room."),
]


_ORDINALS = {"first": 1, "top": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8}


def normalize_region(text: str) -> str:
    """Spoken shelf references -> the Coach's region ids: 'shelf2', 'second shelf', 'shelf row 2', 'top shelf' -> 'row2'.
    Anything else is kept as said."""
    t = text.strip().lower()
    num = re.search(r"\d+", t)
    if num:
        return f"row{int(num.group())}"
    word = next((n for w, n in _ORDINALS.items() if re.search(rf"\b{w}\b", t)), None)
    return f"row{word}" if word else text.strip()


def execute_tool(hooks, name: str, a: dict) -> dict:
    """What every voice tool does to the app (shared by both voice engines). Writes USER FACTS only.
    Half-filled calls (seen with background chatter) are rejected so the model asks instead of guessing."""
    missing = [k for k in {"record_user_fact": ("fact",), "mark_region": ("region", "action")}.get(name, ())
               if not str(a.get(k) or "").strip()]
    if missing:
        return {"ok": False, "error": f"missing {', '.join(missing)}; ask the user which one they mean"}
    if name == "record_user_fact":
        hint = a.get("target_hint")
        if hint and re.search(r"\b(shelf|row)\b", hint.lower()):   # "second shelf" -> "row2"; titles stay as said
            hint = normalize_region(hint)
        hooks.on_fact(hint, a["fact"], None)
    elif name == "mark_region":
        hooks.on_skip(normalize_region(a["region"]), a["action"].strip().lower())
    elif name == "finish_sweep":
        asyncio.create_task(hooks.on_finish())
    else:
        return {"ok": False, "error": "unknown tool"}
    return {"ok": True}


def instructions(meta: dict) -> str:
    """The agent's rules (backend/prompts/agent.py: SYSTEM), filled with this sweep's country and currency."""
    return fill(prompts.SYSTEM, country=meta.get("country") or "unknown", currency=meta.get("currency") or "unknown")


def session_config(meta: dict, cfg=CFG) -> dict:
    """Sent once, when the call is created. Audio format is negotiated by WebRTC, so none is set here."""
    audio_in: dict = {
        "transcription": {"model": cfg.transcribe_model},
        # server_vad (not semantic_vad) because it has a threshold: the knob that ignores background noise
        "turn_detection": {"type": "server_vad", "threshold": cfg.vad_threshold, "silence_duration_ms": cfg.vad_silence_ms,
                           "prefix_padding_ms": cfg.vad_prefix_ms, "create_response": True, "interrupt_response": True},
    }
    if cfg.noise_reduction in ("far_field", "near_field"):
        audio_in["noise_reduction"] = {"type": cfg.noise_reduction}
    return {"type": "realtime", "model": cfg.live_model, "instructions": instructions(meta),
            "output_modalities": ["audio"], "tools": TOOLS, "tool_choice": "auto",
            "audio": {"input": audio_in, "output": {"voice": cfg.voice}}}


async def start_call(sdp_offer: str, meta: dict, cfg=CFG, client=None) -> tuple[str, str]:
    """Create the WebRTC call with OpenAI. -> (sdp_answer, call_id)."""
    from openai import AsyncOpenAI
    client = client or AsyncOpenAI()
    res = await client.realtime.calls.create(sdp=sdp_offer, session=session_config(meta, cfg))
    call_id = res.response.headers.get("location", "").rstrip("/").rsplit("/", 1)[-1]
    if not call_id:
        raise RuntimeError("OpenAI did not return a call id")
    return res.text, call_id


async def hangup(call_id: str, client=None) -> None:
    from openai import AsyncOpenAI
    try:
        await (client or AsyncOpenAI()).realtime.calls.hangup(call_id)
    except Exception as e:                       # already gone is fine
        log.info("hangup %s: %s", call_id, e)


class RealtimeAgent:
    """Sideband controller for one call: turns server events into UI messages and runs the tools."""

    def __init__(self, session, call_id: str, cfg=CFG):
        self.s, self.call_id, self.cfg = session, call_id, cfg
        self.idle = asyncio.Event()           # set when no response is being generated
        self.idle.set()
        self.pending: list[dict] = []         # tool results to send once the current response is done
        self.state, self.user_speaking = "listening", False

    async def _state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            await self.s.down.put({"type": "agent_state", "state": state})

    async def _caption(self, role: str, item_id: str, text: str, mode: str) -> None:
        await self.s.down.put({"type": "transcript", "role": role, "item_id": item_id, "text": text, "mode": mode})

    async def handle(self, ev, conn) -> None:
        t = ev.type
        if t in ("output_audio_buffer.started", "response.output_audio.delta"):   # first = WebRTC playback started
            await self._state("speaking")
        elif t == "output_audio_buffer.stopped":                                  # phone finished playing the reply
            await self._state("hearing" if self.user_speaking else "listening")
        elif t == "response.output_audio_transcript.delta":
            await self._state("speaking")
            await self._caption("agent", ev.item_id, ev.delta, "append")
        elif t == "response.output_audio_transcript.done":
            self.s.transcript.append("agent: " + ev.transcript)
            await self._caption("agent", ev.item_id, ev.transcript, "set")
        elif t == "input_audio_buffer.speech_started":
            self.user_speaking = True
            self.s.last_user_speech_t = self.s.now()
            await self._caption("user", ev.item_id, "", "set")     # placeholder bubble, filled in below
            await self._state("hearing")
        elif t == "input_audio_buffer.speech_stopped":
            self.user_speaking = False
            await self._state("thinking")
        elif t == "conversation.item.input_audio_transcription.delta":
            await self._caption("user", ev.item_id, ev.delta, "append")
        elif t == "conversation.item.input_audio_transcription.completed":
            self.s.transcript.append("user: " + ev.transcript)
            await self._caption("user", ev.item_id, ev.transcript, "set")
        elif t == "response.created":
            self.idle.clear()
            if not self.user_speaking:
                await self._state("thinking")
        elif t == "response.function_call_arguments.done":
            self.pending.append({"call_id": ev.call_id, "output": self._run_tool(ev.name, ev.arguments)})
        elif t == "response.done":
            self.idle.set()
            if self.pending:                                    # give tool results back, let the model continue
                for p in self.pending:
                    await conn.conversation.item.create(item={"type": "function_call_output", **p})
                self.pending = []
                await conn.response.create()
            elif self.state == "thinking":                      # nothing was said (e.g. noise): back to listening
                await self._state("hearing" if self.user_speaking else "listening")
        elif t == "error":
            err = getattr(ev, "error", ev)
            log.warning("realtime error: %s", err)
            await self.s.down.put({"type": "status", "text": f"Voice error: {getattr(err, 'message', err)}"})

    def _run_tool(self, name: str, arguments: str) -> str:
        try:
            a = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            a = {}
        return json.dumps(execute_tool(self.s.hooks, name, a))

    async def _pump_inject(self, conn) -> None:
        """App-state messages, marked as such so they are not mistaken for the user speaking."""
        while True:
            text = await self.s.inject.get()
            await self.idle.wait()
            await conn.conversation.item.create(item={"type": "message", "role": "user",
                                                      "content": [{"type": "input_text", "text": f"[app-state] {text}"}]})
            await conn.response.create()

    async def _events(self, conn) -> None:
        async for ev in conn:
            await self.handle(ev, conn)

    async def run(self, client=None) -> None:
        """Stay attached to the call until cancelled; re-attach if the sideband socket drops (the call survives)."""
        from openai import AsyncOpenAI
        client = client or AsyncOpenAI()
        for attempt in range(5):
            try:
                async with client.realtime.connect(call_id=self.call_id) as conn:
                    await self.s.down.put({"type": "status", "text": "voice connected"})
                    await asyncio.gather(self._pump_inject(conn), self._events(conn))
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("sideband dropped (%s); retry %d", e, attempt + 1)
                await self.s.down.put({"type": "status", "text": f"Voice reconnecting ({type(e).__name__})"})
                await asyncio.sleep(1 + attempt)


async def run(session, call_id: str, cfg=CFG) -> None:
    """Entry point used by live.py after start_call (swappable in create_app for tests)."""
    await RealtimeAgent(session, call_id, cfg).run()
