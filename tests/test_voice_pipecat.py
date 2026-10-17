"""Pipecat voice engine: UI bridge (frames -> captions/state), tools, and engine selection. No network."""
import asyncio
import dataclasses
from types import SimpleNamespace as NS

from pipecat.frames import frames as F
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frame_processor import FrameDirection

from backend.config import CFG
from backend.live import agent, voice_pipecat
from backend.live.app import SweepSession, create_app
from backend.live.coach import Coach


class Hooks:
    def __init__(self): self.facts, self.skips, self.finished = [], [], 0
    def on_fact(self, ref, text, turn): self.facts.append((ref, text))
    def on_skip(self, region, action): self.skips.append((region, action))
    async def on_finish(self): self.finished += 1


def session(tmp_path):
    s = SweepSession("sw_x", tmp_path, CFG, Coach(CFG)); s.hooks = Hooks()
    return s


def push(frame, first=True, direction=FrameDirection.DOWNSTREAM):
    return FramePushed(source=None, destination=None, frame=frame, direction=direction, timestamp=0, first_push=first)


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_bridge_turns_frames_into_captions_and_state(tmp_path):
    async def go():
        s = session(tmp_path); b = voice_pipecat._make_bridge(s)
        up = F.TranscriptionFrame("skip shelf 2", "", "t")
        up.broadcast_sibling_id = "x"
        for p in [push(F.UserStartedSpeakingFrame()), push(F.UserStoppedSpeakingFrame()),
                  push(F.TranscriptionFrame("skip shelf 2", "", "t")),
                  push(up, direction=FrameDirection.UPSTREAM),                 # broadcast copy: ignored
                  push(F.LLMFullResponseStartFrame()), push(F.BotStartedSpeakingFrame()),
                  push(F.TTSTextFrame("Not", aggregated_by="word")), push(F.TTSTextFrame("ed, skipping.", aggregated_by="word")),
                  push(F.TTSTextFrame("Not", aggregated_by="word"), first=False),   # later hop of a frame: ignored
                  push(F.LLMFullResponseEndFrame()), push(F.BotStoppedSpeakingFrame())]:
            await b.on_push_frame(p)
        return s, drain(s.down)
    s, msgs = asyncio.run(go())
    assert [m["state"] for m in msgs if m["type"] == "agent_state"] == ["hearing", "thinking", "speaking", "listening"]
    caps = [(m["role"], m["item_id"], m["text"]) for m in msgs if m["type"] == "transcript"]
    assert ("user", "u1", "skip shelf 2") in caps and caps[-1] == ("agent", "a1", "Noted, skipping.")
    assert s.transcript == ["user: skip shelf 2", "agent: Noted, skipping."]


def test_harmless_truncate_error_is_not_shown_but_real_errors_are(tmp_path):
    async def go():
        s = session(tmp_path); b = voice_pipecat._make_bridge(s)
        await b.on_push_frame(push(F.ErrorFrame("Audio content of 150ms is already shorter than 1123ms")))
        await b.on_push_frame(push(F.ErrorFrame("invalid api key")))
        return drain(s.down)
    msgs = asyncio.run(go())
    assert [m["text"] for m in msgs] == ["Voice error: invalid api key"]


def test_tools_share_the_same_rules_and_build_a_schema(tmp_path):
    from pipecat.processors.aggregators.llm_context import LLMContext
    s = session(tmp_path)
    tools = voice_pipecat.make_tools(s.hooks)
    LLMContext([], tools)                                                     # Pipecat accepts them as tools
    results = []
    async def go():
        cb = lambda r: results.append(r) or asyncio.sleep(0)
        await tools[1](NS(result_callback=cb), region="second shelf", action="skip")
        await tools[0](NS(result_callback=cb), target_hint="b_001", fact="")  # half-filled: rejected
    asyncio.run(go())
    assert s.hooks.skips == [("row2", "skip")] and results[0] == {"ok": True} and results[1]["ok"] is False


def test_engine_is_chosen_by_config(tmp_path):
    assert create_app("t", dataclasses.replace(CFG, voice_engine="direct", sweeps_dir=tmp_path)).state.voice is agent
    assert create_app("t", dataclasses.replace(CFG, voice_engine="pipecat", sweeps_dir=tmp_path)).state.voice is voice_pipecat


# ---- app messages must be SPOKEN, not just accepted (Pipecat's Realtime service stores a context update silently)
class FakeLLM:
    def __init__(self, fail_create=False):
        self.events, self.created, self.fail_create = [], 0, fail_create

    async def send_client_event(self, evt): self.events.append(evt)

    async def _create_response(self):
        if self.fail_create:
            raise RuntimeError("renamed in a newer Pipecat")
        self.created += 1


def test_an_app_message_becomes_a_user_message_and_asks_for_a_response():
    import asyncio
    from backend.live.voice_pipecat import say
    llm = FakeLLM()
    asyncio.run(say(llm, "[app-state] CAPTURE_ISSUE ask=\"Hold steady.\""))
    item = llm.events[0].item
    assert llm.events[0].type == "conversation.item.create" and item.role == "user"
    assert item.content[0].text == "[app-state] CAPTURE_ISSUE ask=\"Hold steady.\"" and llm.created == 1


def test_if_the_private_response_call_goes_away_a_plain_response_create_is_sent():
    import asyncio
    from backend.live.voice_pipecat import say
    llm = FakeLLM(fail_create=True)
    asyncio.run(say(llm, "x"))
    assert [e.type for e in llm.events] == ["conversation.item.create", "response.create"]


def test_the_pump_waits_until_nobody_is_talking_and_survives_a_failed_delivery():
    import asyncio
    from types import SimpleNamespace
    from backend.live.voice_pipecat import _pump_inject

    class Flaky(FakeLLM):
        async def send_client_event(self, evt):
            if not self.events and not getattr(self, "boom", False):
                self.boom = True
                raise ConnectionError("socket closed")
            await super().send_client_event(evt)

    async def go():
        session, bridge, llm = SimpleNamespace(inject=asyncio.Queue()), SimpleNamespace(state="speaking"), Flaky()
        task = asyncio.create_task(_pump_inject(session, llm, bridge))
        session.inject.put_nowait("first (lost)")
        session.inject.put_nowait("second")
        await asyncio.sleep(0.5)
        assert llm.events == []                                   # the agent is still speaking: nothing is sent over it
        bridge.state = "listening"
        await asyncio.sleep(0.6)
        task.cancel()
        return llm
    llm = asyncio.run(go())
    assert [e.item.content[0].text for e in llm.events if e.type == "conversation.item.create"] == ["[app-state] second"]
