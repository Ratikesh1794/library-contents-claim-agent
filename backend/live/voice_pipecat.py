"""Voice agent, Pipecat engine (VOICE_ENGINE=pipecat, the default). Same interface as backend/live/agent.py
(start_call / run / hangup), so backend/live/app.py can use either.

    phone mic ──WebRTC (same Wi-Fi)──► laptop: SmallWebRTC ──► RNNoise ──► Silero VAD (volume gate) ──► OpenAI Realtime
    phone speaker ◄──────────────────────────────────────────────────────────────────────────────────┘

Why: with OpenAI's own turn detection, any sound that reaches it can start a turn or cut the agent off. Here
the laptop cleans the audio first and decides locally when the user speaks:
  * RNNoise removes steady noise (fans, traffic, hiss)                          NOISE_FILTER=rnnoise|off
  * Silero VAD needs `vad_min_volume` loudness: the user speaks close to the phone, a TV across the room
    is much quieter, so it no longer counts as the user talking                  VAD_MIN_VOLUME (0..1)
  * speech must last `vad_start_secs` before it counts, so a cough or a bang doesn't interrupt
  * OpenAI's server-side turn detection is OFF (turn_detection=False); far_field noise reduction stays on.
Limits: RNNoise is not voice isolation. A loud TV close to the phone can still pass the volume gate; for true
voice isolation Pipecat supports Krisp VIVA / ai-coustics (commercial licences), and hold-to-talk still works.

Network: the phone's audio goes to the laptop over WebRTC (peer to peer); ngrok only carries the page and the
signalling, so the phone must reach the laptop directly (same Wi-Fi). Set ICE_SERVERS for a TURN relay otherwise.
"""
from __future__ import annotations

import asyncio
import logging
import os

from backend.config import CFG
from backend.live.agent import execute_tool, instructions

log = logging.getLogger(__name__)

_CALLS: dict[str, tuple] = {}          # call_id (Pipecat pc_id) -> (SmallWebRTCConnection, sweep meta)


async def start_call(sdp_offer: str, meta: dict, cfg=CFG) -> tuple[str, str]:
    """Answer the phone's WebRTC offer with a laptop-side peer. -> (sdp_answer, call_id)."""
    from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
    conn = SmallWebRTCConnection(ice_servers=list(cfg.ice_servers) or None)
    await conn.initialize(sdp=sdp_offer, type="offer")
    answer = conn.get_answer()
    if not answer:
        raise RuntimeError("Pipecat produced no SDP answer")
    _CALLS[answer["pc_id"]] = (conn, meta)
    return answer["sdp"], answer["pc_id"]


async def hangup(call_id: str) -> None:
    entry = _CALLS.pop(call_id, None)
    if entry:
        try:
            await entry[0].disconnect()
        except Exception as e:          # already gone is fine
            log.info("hangup %s: %s", call_id, e)


def make_tools(hooks) -> list:
    """The agent's tools as plain functions: Pipecat builds the tool schema from each signature + docstring.
    Execution is shared with the other engine (agent.execute_tool), including rejecting half-filled calls."""

    async def record_user_fact(params, target_hint: str, fact: str):
        """Record something the user told you about an item (e.g. 'first edition', 'signed', 'print').

        Args:
            target_hint: What it refers to: item id, shelf row, or title words.
            fact: What the user said.
        """
        await params.result_callback(execute_tool(hooks, "record_user_fact", {"target_hint": target_hint, "fact": fact}))

    async def mark_region(params, region: str, action: str):
        """Skip or include a shelf region on the user's instruction.

        Args:
            region: Region id, e.g. row2.
            action: skip or include.
        """
        await params.result_callback(execute_tool(hooks, "mark_region", {"region": region, "action": action}))

    async def finish_sweep(params):
        """Call when the user says they are done walking the room."""
        await params.result_callback(execute_tool(hooks, "finish_sweep", {}))

    return [record_user_fact, mark_region, finish_sweep]


def _make_bridge(session):
    """Observer that turns pipeline frames into the UI messages the phone already understands
    (transcript captions + agent state), and keeps the session transcript for transcript.json."""
    from pipecat.frames import frames as F
    from pipecat.observers.base_observer import BaseObserver
    from pipecat.processors.frame_processor import FrameDirection

    class UIBridge(BaseObserver):
        def __init__(self):
            super().__init__()
            self.state, self.user_turn, self.bot_turn, self.user_speaking, self.bot_text = "listening", 0, 0, False, ""

        async def _state(self, state: str):
            if state != self.state:
                self.state = state
                await session.down.put({"type": "agent_state", "state": state})

        async def _caption(self, role: str, item_id: str, text: str, mode: str):
            await session.down.put({"type": "transcript", "role": role, "item_id": item_id, "text": text, "mode": mode})

        async def on_push_frame(self, data):
            f = data.frame
            if not data.first_push:                                    # every hop re-pushes the frame
                return
            if getattr(f, "broadcast_sibling_id", None) and data.direction == FrameDirection.UPSTREAM:
                return                                                 # broadcast copy: handle one direction only
            if isinstance(f, F.UserStartedSpeakingFrame):
                self.user_turn += 1
                self.user_speaking = True
                session.last_user_speech_t = session.now()
                await self._caption("user", f"u{self.user_turn}", "", "set")
                await self._state("hearing")
            elif isinstance(f, F.UserStoppedSpeakingFrame):
                self.user_speaking = False
                await self._state("thinking")
            elif isinstance(f, F.InterimTranscriptionFrame):
                await self._caption("user", f"u{self.user_turn}", f.text, "append")
            elif isinstance(f, F.TranscriptionFrame):
                session.transcript.append("user: " + f.text)
                await self._caption("user", f"u{self.user_turn}", f.text, "set")
            elif isinstance(f, F.LLMFullResponseStartFrame):
                self.bot_turn += 1
                self.bot_text = ""
            elif isinstance(f, F.TTSTextFrame):
                self.bot_text += f.text                    # pieces carry their own spacing ("Not" + "ed", " Thanks")
                await self._caption("agent", f"a{self.bot_turn}", self.bot_text, "set")
            elif isinstance(f, F.LLMFullResponseEndFrame):
                if self.bot_text.strip():
                    session.transcript.append("agent: " + self.bot_text.strip())
            elif isinstance(f, F.BotStartedSpeakingFrame):
                await self._state("speaking")
            elif isinstance(f, F.BotStoppedSpeakingFrame):
                await self._state("hearing" if self.user_speaking else "listening")
            elif isinstance(f, F.ErrorFrame):
                if "already shorter than" in str(f.error):             # harmless: truncating an interrupted reply
                    log.info("realtime truncate after interruption: %s", f.error)
                else:
                    await session.down.put({"type": "status", "text": f"Voice error: {f.error}"})

    return UIBridge()


async def _pump_inject(session, worker, bridge) -> None:
    """Coach / app-state messages, marked [app-state], sent only while nobody is talking."""
    from pipecat.frames.frames import LLMMessagesAppendFrame
    while True:
        text = await session.inject.get()
        while bridge.state in ("hearing", "thinking", "speaking"):
            await asyncio.sleep(0.2)
        await worker.queue_frames([LLMMessagesAppendFrame(
            messages=[{"role": "user", "content": f"[app-state] {text}"}], run_llm=True)])


async def run(session, call_id: str, cfg=CFG) -> None:
    """Build the pipeline for this call and run it until the phone disconnects or the task is cancelled."""
    from pipecat.audio.filters.rnnoise_filter import RNNoiseFilter
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams
    from pipecat.frames.frames import LLMRunFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
    from pipecat.services.openai.realtime.events import (AudioConfiguration, AudioInput, AudioOutput,
                                                         InputAudioNoiseReduction, InputAudioTranscription,
                                                         SessionProperties)
    from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
    from pipecat.transports.base_transport import TransportParams
    from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
    from pipecat.workers.runner import WorkerRunner

    entry = _CALLS.get(call_id)
    if entry is None:
        return
    conn, meta = entry
    transport = SmallWebRTCTransport(webrtc_connection=conn, params=TransportParams(
        audio_in_enabled=True, audio_out_enabled=True,
        audio_in_filter=RNNoiseFilter() if cfg.noise_filter == "rnnoise" else None))
    audio_in = AudioInput(transcription=InputAudioTranscription(model=cfg.transcribe_model), turn_detection=False,
                          noise_reduction=InputAudioNoiseReduction(type=cfg.noise_reduction)
                          if cfg.noise_reduction in ("far_field", "near_field") else None)
    llm = OpenAIRealtimeLLMService(api_key=os.environ["OPENAI_API_KEY"], settings=OpenAIRealtimeLLMService.Settings(
        model=cfg.live_model, system_instruction=instructions(meta),
        session_properties=SessionProperties(audio=AudioConfiguration(input=audio_in, output=AudioOutput(voice=cfg.voice)))))
    context = LLMContext([{"role": "developer", "content": "Greet the user now, following your rules."}],
                         make_tools(session.hooks))
    user_agg, assistant_agg = LLMContextAggregatorPair(context, user_params=LLMUserAggregatorParams(
        vad_analyzer=SileroVADAnalyzer(params=VADParams(
            confidence=cfg.vad_confidence, start_secs=cfg.vad_start_secs,
            stop_secs=cfg.vad_stop_secs, min_volume=cfg.vad_min_volume))))
    bridge = _make_bridge(session)
    worker = PipelineWorker(Pipeline([transport.input(), user_agg, llm, transport.output(), assistant_agg]),
                            params=PipelineParams(), observers=[bridge],
                            processor_unusable_policy=ProcessorUnusablePolicy.END)
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)

    @transport.event_handler("on_client_connected")
    async def _connected(_transport, _client):
        await session.down.put({"type": "status", "text": "voice connected"})
        await worker.queue_frames([LLMRunFrame()])                    # the greeting

    @transport.event_handler("on_client_disconnected")
    async def _disconnected(_transport, _client):
        await runner.cancel()

    inject = asyncio.create_task(_pump_inject(session, worker, bridge))
    try:
        await runner.run()
    except asyncio.CancelledError:
        await runner.cancel()
        raise
    finally:
        inject.cancel()
        _CALLS.pop(call_id, None)
