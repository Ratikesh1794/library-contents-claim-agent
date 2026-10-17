import asyncio
from types import SimpleNamespace as NS

from backend.live import agent
from backend.live.coach import Coach
from backend.config import CFG
from backend.live.app import SweepSession


class FakeConn:
    def __init__(self):
        self.sent = []
        self.conversation = NS(item=NS(create=self._item))
        self.response = NS(create=self._resp)

    async def _item(self, item): self.sent.append(("item", item))
    async def _resp(self): self.sent.append(("response.create", None))


class Hooks:
    def __init__(self): self.facts, self.skips, self.finished = [], [], 0
    def on_fact(self, ref, text, turn): self.facts.append((ref, text))
    def on_skip(self, region, action): self.skips.append((region, action))
    async def on_finish(self): self.finished += 1


def make(tmp_path):
    s = SweepSession("sw_x", tmp_path, CFG, Coach(CFG)); s.hooks = Hooks()
    return s, agent.RealtimeAgent(s, "rtc_test", CFG), FakeConn()


def run(coro): return asyncio.run(coro)


def test_session_config_is_valid_for_the_sdk_and_tuned_for_noise():
    from openai.types.realtime import RealtimeSessionCreateRequest
    c = agent.session_config({"country": "IN", "currency": "INR"}, CFG)
    RealtimeSessionCreateRequest.model_validate(c)                     # same schema the server enforces
    a_in = c["audio"]["input"]
    assert a_in["noise_reduction"] == {"type": "far_field"}
    assert a_in["turn_detection"]["type"] == "server_vad" and a_in["turn_detection"]["threshold"] == CFG.vad_threshold
    assert "format" not in a_in and "IN" in c["instructions"] and "INR" in c["instructions"]
    assert {t["name"] for t in agent.TOOLS} == {"record_user_fact", "mark_region", "set_locale", "finish_sweep"}
    import dataclasses
    off = agent.session_config({}, dataclasses.replace(CFG, noise_reduction="off"))
    assert "noise_reduction" not in off["audio"]["input"]


def test_start_call_returns_answer_and_call_id():
    class Calls:
        async def create(self, sdp, session):
            self.sent = (sdp, session)
            return NS(text="v=0 answer", response=NS(headers={"location": "/v1/realtime/calls/rtc_abc123"}))
    calls = Calls()
    answer, call_id = run(agent.start_call("v=0 offer", {"country": "US"}, CFG, client=NS(realtime=NS(calls=calls))))
    assert (answer, call_id) == ("v=0 answer", "rtc_abc123") and calls.sent[0] == "v=0 offer"


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_captions_stream_and_state_follows_the_conversation(tmp_path):
    async def go():
        s, a, c = make(tmp_path)
        H = lambda t, **kw: a.handle(NS(type=t, **kw), c)
        await H("input_audio_buffer.speech_started", item_id="u1")
        await H("conversation.item.input_audio_transcription.delta", item_id="u1", delta="skip ")
        await H("input_audio_buffer.speech_stopped", item_id="u1")
        await H("conversation.item.input_audio_transcription.completed", item_id="u1", transcript="skip shelf 2")
        await H("response.created")
        await H("response.output_audio_transcript.delta", item_id="a1", delta="Sure, ")
        await H("response.output_audio.delta", delta="QUJD")
        await H("response.output_audio_transcript.done", item_id="a1", transcript="Sure, skipping shelf 2.")
        await H("response.done")                       # generation done, phone may still be playing...
        await H("output_audio_buffer.stopped")         # ...playback finished: now listening
        return s, drain(s.down)
    s, msgs = run(go())
    caps = [(m["role"], m["item_id"], m["text"], m["mode"]) for m in msgs if m["type"] == "transcript"]
    assert caps == [("user", "u1", "", "set"), ("user", "u1", "skip ", "append"), ("user", "u1", "skip shelf 2", "set"),
                    ("agent", "a1", "Sure, ", "append"), ("agent", "a1", "Sure, skipping shelf 2.", "set")]
    assert [m["state"] for m in msgs if m["type"] == "agent_state"] == ["hearing", "thinking", "speaking", "listening"]
    assert not any(m["type"] in ("audio", "interrupted") for m in msgs)   # audio is WebRTC now; never relayed by us
    assert s.transcript == ["user: skip shelf 2", "agent: Sure, skipping shelf 2."] and s.last_user_speech_t > float("-inf")


def test_webrtc_playback_events_drive_speaking_state(tmp_path):
    async def go():
        s, a, c = make(tmp_path)
        await a.handle(NS(type="output_audio_buffer.started"), c)
        await a.handle(NS(type="output_audio_buffer.stopped"), c)
        return [m["state"] for m in drain(s.down) if m["type"] == "agent_state"]
    assert run(go()) == ["speaking", "listening"]


def test_noise_that_gets_no_reply_returns_to_listening(tmp_path):
    async def go():
        s, a, c = make(tmp_path)
        await a.handle(NS(type="input_audio_buffer.speech_started", item_id="n1"), c)
        await a.handle(NS(type="input_audio_buffer.speech_stopped", item_id="n1"), c)
        await a.handle(NS(type="response.created"), c)
        await a.handle(NS(type="response.done"), c)                 # model chose to say nothing
        return a.state
    assert run(go()) == "listening"


def test_barge_in_during_reply_keeps_state_hearing(tmp_path):
    async def go():
        s, a, c = make(tmp_path)
        H = lambda t, **kw: a.handle(NS(type=t, **kw), c)
        await H("response.created"); await H("response.output_audio.delta", delta="x")
        await H("input_audio_buffer.speech_started", item_id="u2")
        await H("response.done")                                   # the cancelled reply ends while user still talks
        return a.state
    assert run(go()) == "hearing"


def test_tool_calls_run_and_results_return_only_after_response_done(tmp_path):
    async def go():
        s, a, c = make(tmp_path)
        await a.handle(NS(type="response.created"), c)
        assert not a.idle.is_set()
        await a.handle(NS(type="response.function_call_arguments.done", call_id="c1", name="record_user_fact",
                          arguments='{"target_hint":"b_001","fact":"signed"}'), c)
        await a.handle(NS(type="response.function_call_arguments.done", call_id="c2", name="mark_region",
                          arguments='{"region":"row2","action":"skip"}'), c)
        await a.handle(NS(type="response.function_call_arguments.done", call_id="c3", name="finish_sweep", arguments="{}"), c)
        assert c.sent == []                                        # nothing sent mid-response
        await a.handle(NS(type="response.done"), c)
        await asyncio.sleep(0)                                     # let the finish task run
        return s, a, c
    s, a, c = run(go())
    assert s.hooks.facts == [("b_001", "signed")] and s.hooks.skips == [("row2", "skip")] and s.hooks.finished == 1
    kinds = [k for k, _ in c.sent]
    assert kinds == ["item", "item", "item", "response.create"] and c.sent[0][1]["call_id"] == "c1"
    assert a.idle.is_set()


def test_unknown_tool_is_rejected_not_executed(tmp_path):
    _, a, _ = make(tmp_path)
    assert '"ok": false' in a._run_tool("delete_everything", "{}")


def test_app_state_is_marked_and_waits_for_idle(tmp_path):
    async def go():
        s, a, c = make(tmp_path)
        a.idle.clear()
        await s.inject.put("CAPTURE_ISSUE region=row1 reason=blur ask=\"Hold steady\"")
        t = asyncio.create_task(a._pump_inject(c))
        await asyncio.sleep(0.05)
        waited = list(c.sent)
        a.idle.set(); await asyncio.sleep(0.05); t.cancel()
        return waited, c.sent
    waited, sent = run(go())
    assert waited == [] and sent[0][1]["content"][0]["text"].startswith("[app-state] CAPTURE_ISSUE")


def test_half_filled_tool_calls_are_rejected_not_recorded(tmp_path):
    s, a, _ = make(tmp_path)
    assert '"ok": false' in a._run_tool("mark_region", '{"region": "", "action": "skip"}')
    assert '"ok": false' in a._run_tool("record_user_fact", '{"target_hint": "b_001"}')
    assert s.hooks.skips == [] and s.hooks.facts == []
    assert '"ok": true' in a._run_tool("mark_region", '{"region": "row2", "action": "skip"}') and s.hooks.skips == [("row2", "skip")]


def test_spoken_shelf_names_map_to_coach_rows(tmp_path):
    for said in ("shelf2", "second shelf", "shelf row 2", "Row 2"):
        assert agent.normalize_region(said) == "row2"
    assert agent.normalize_region("top shelf") == "row1" and agent.normalize_region("by the window") == "by the window"
    s, a, _ = make(tmp_path)
    a._run_tool("mark_region", '{"region": "shelf2", "action": "Skip"}')
    a._run_tool("record_user_fact", '{"target_hint": "second shelf", "fact": "all signed"}')
    a._run_tool("record_user_fact", '{"target_hint": "Dune", "fact": "first edition"}')
    assert s.hooks.skips == [("row2", "skip")] and s.hooks.facts == [("row2", "all signed"), ("Dune", "first edition")]


def test_set_locale_records_what_the_user_said_and_rejects_guesses():
    class H:
        got = None
        def on_locale(self, country, currency): H.got = (country, currency)
    assert agent.execute_tool(H(), "set_locale", {"country": "uk", "currency": "gbp"})["ok"] and H.got == ("GB", "GBP")
    assert not agent.execute_tool(H(), "set_locale", {"country": "Britain", "currency": "pounds"})["ok"]
    assert not agent.execute_tool(H(), "set_locale", {"country": "GB"})["ok"]                   # half-filled: ask
    assert "unknown" in agent.instructions({"country": None, "currency": None})
