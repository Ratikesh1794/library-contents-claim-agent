import dataclasses

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.config import CFG
from backend.live.app import create_app
from backend.ports import Detection, SpineRead
from tests.fakes import FakeCatalogue, FakeVision, cand

TOK = "secret-token"


class FakeVoice:
    """Stands in for backend.agent: no real OpenAI call is ever made in tests."""
    def __init__(self): self.calls, self.hung_up, self.ran = 0, [], []
    async def start_call(self, sdp, meta, cfg):
        self.calls += 1
        return "v=0 answer", f"rtc_{self.calls}"
    async def run(self, session, call_id, cfg): self.ran.append(call_id)
    async def hangup(self, call_id): self.hung_up.append(call_id)


@pytest.fixture
def client(tmp_path):
    cfg = dataclasses.replace(CFG, sweeps_dir=tmp_path, keyframe_max_bytes=300_000)
    dets = [Detection((100, 50, 160, 450), "book", "upright")]
    v = FakeVision(dets, reads=[SpineRead("Dune", "Frank Herbert", None, True)] * 4)
    app = create_app(TOK, cfg, vision=v, catalogue=FakeCatalogue({"dune": [cand("Dune", "Frank Herbert")]}), voice=FakeVoice())
    with TestClient(app) as c:
        yield c


def jpeg(n=100):
    ok, buf = cv2.imencode(".jpg", np.random.default_rng(0).integers(0, 255, (700, n, 3), dtype=np.uint8))
    return buf.tobytes()


H = {"X-Sweep-Token": TOK}


def test_requests_without_token_are_rejected(client):
    assert client.post("/api/sweeps", json={}).status_code == 401
    assert client.post("/api/sweeps", json={}, headers={"X-Sweep-Token": "wrong"}).status_code == 401
    assert client.get("/").status_code == 401
    assert client.get(f"/?t={TOK}").status_code == 200


def test_same_origin_api_call_accepted_and_frame_flow(client):
    sid = client.post("/api/sweeps", json={"country": "US", "currency": "USD"}, headers=H).json()["sweep_id"]
    r = client.post(f"/api/sweeps/{sid}/frames?seq=0&t=0.5&w=600&h=700", content=jpeg(), headers=H)
    assert r.status_code == 200 and r.json()["frame_id"] == "f_0000"
    again = client.post(f"/api/sweeps/{sid}/frames?seq=0&t=0.5&w=600&h=700", content=jpeg(), headers=H)
    assert again.json().get("duplicate")                       # re-upload after reconnect is idempotent
    assert client.get(f"/api/sweeps/{sid}/frames/f_0000.jpg", headers=H).status_code == 200
    assert client.get(f"/api/sweeps/{sid}/frames/..%2f..%2fmeta.json", headers=H).status_code == 404


def test_oversized_keyframe_rejected(client):
    sid = client.post("/api/sweeps", json={}, headers=H).json()["sweep_id"]
    r = client.post(f"/api/sweeps/{sid}/frames?seq=1&t=1", content=b"x" * 400_000, headers=H)
    assert r.status_code == 413


def test_websocket_wrong_token_closed_and_right_token_gets_inventory(client):
    sid = client.post("/api/sweeps", json={}, headers=H).json()["sweep_id"]
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(f"/ws/live?sweep={sid}&t=bad"):
            pass
    assert e.value.code == 4401
    with client.websocket_connect(f"/ws/live?sweep={sid}&t={TOK}") as ws:
        kinds = [ws.receive_json()["type"] for _ in range(2)]
        assert kinds == ["phone", "inventory"]                       # presence, then the current inventory


def test_voice_offer_returns_answer_and_replaces_previous_call(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    sid = client.post("/api/sweeps", json={"country": "US"}, headers=H).json()["sweep_id"]
    hdr = {**H, "Content-Type": "application/sdp"}
    r = client.post(f"/api/sweeps/{sid}/voice", content="v=0 offer", headers=hdr)
    assert r.status_code == 200 and r.text == "v=0 answer" and r.headers["content-type"].startswith("application/sdp")
    client.post(f"/api/sweeps/{sid}/voice", content="v=0 offer again", headers=hdr)   # e.g. phone reconnects
    voice = client.app.state.voice
    for _ in range(50):                                # sideband + hangup run as background tasks
        if voice.ran == ["rtc_1", "rtc_2"] and voice.hung_up == ["rtc_1"]:
            break
        client.get("/static/app.js")                   # give the app's event loop a turn
    assert voice.ran == ["rtc_1", "rtc_2"] and voice.hung_up == ["rtc_1"]


def test_voice_rejects_bad_offer_missing_token_and_missing_key(client, monkeypatch):
    sid = client.post("/api/sweeps", json={}, headers=H).json()["sweep_id"]
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    assert client.post(f"/api/sweeps/{sid}/voice", content="v=0").status_code == 401
    assert client.post(f"/api/sweeps/{sid}/voice", content="not sdp", headers=H).status_code == 400
    monkeypatch.delenv("OPENAI_API_KEY")
    assert client.post(f"/api/sweeps/{sid}/voice", content="v=0", headers=H).status_code == 503


def test_rate_limits_return_429(client):
    sid = client.post("/api/sweeps", json={}, headers=H).json()["sweep_id"]
    codes = [client.post(f"/api/sweeps/{sid}/frames?seq={i}&t={i}", content=jpeg(), headers=H).status_code for i in range(7)]
    assert codes[:5] == [200] * 5 and 429 in codes[5:]              # 5 frames per second per sweep
    assert [client.post(f"/api/sweeps/{sid}/finish", headers=H).status_code for _ in range(4)][-1] == 429


def test_phone_and_dashboard_both_get_every_message_and_dashboard_catches_up(client):
    sid = client.post("/api/sweeps", json={"country": "US"}, headers=H).json()["sweep_id"]
    with client.websocket_connect(f"/ws/live?sweep={sid}&t={TOK}") as phone:
        assert [phone.receive_json()["type"] for _ in range(2)] == ["phone", "inventory"]
        client.post(f"/api/sweeps/{sid}/frames?seq=0&t=0.5&w=100&h=700", content=jpeg(), headers=H)
        seen = []
        while "frame" not in seen:                                  # the frame worker reports the analysed frame
            seen.append(phone.receive_json()["type"])
        with client.websocket_connect(f"/ws/live?sweep={sid}&t={TOK}&role=dashboard") as dash:
            hello = dash.receive_json()
            assert hello["type"] == "hello" and hello["phone"] is True and hello["meta"]["country"] == "US"
            replay = []
            while "frame" not in replay:                            # history: what happened before it opened
                replay.append(dash.receive_json()["type"])
            assert "phone" in replay


def test_sweep_list_and_record_thumbnail(client):
    sid = client.post("/api/sweeps", json={"country": "GB"}, headers=H).json()["sweep_id"]
    client.post(f"/api/sweeps/{sid}/frames?seq=0&t=0&w=100&h=700", content=jpeg(), headers=H)
    sweeps = client.get("/api/sweeps", headers=H).json()
    mine = next(x for x in sweeps if x["id"] == sid)
    assert mine["country"] == "GB" and mine["frames"] == 1 and mine["packet"] is False
    r = client.get(f"/api/sweeps/{sid}/crop?f=f_0000.jpg&b=10,10,60,300&w=120", headers=H)
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/sweeps/{sid}/crop?f=../meta.json&b=1,1,2,2", headers=H).status_code == 404
    assert client.get(f"/api/sweeps/{sid}/crop?f=f_0000.jpg&b=1,1", headers=H).status_code == 400
    assert client.get("/api/sweeps").status_code == 401
    assert client.get(f"/dashboard?t={TOK}").status_code in (200, 500)        # page exists once web/dashboard.html is in


def test_connect_gives_the_phone_link_and_a_qr_code(client):
    assert client.get("/api/connect").status_code == 401
    c = client.get("/api/connect", headers=H).json()
    assert c["phone_url"].endswith(f"/?t={TOK}") and c["qr_svg"].lstrip().startswith("<") and "svg" in c["qr_svg"]
    assert c["https"] is False                                    # test client is plain http: the page warns


def test_the_same_view_sent_again_and_again_stays_one_book_in_the_inventory(client):
    sid = client.post("/api/sweeps", json={"country": "US"}, headers=H).json()["sweep_id"]
    with client.websocket_connect(f"/ws/live?sweep={sid}&t={TOK}") as ws:
        for k in range(3):                                         # the user lingers: three keyframes of the same shelf
            client.post(f"/api/sweeps/{sid}/frames?seq={k}&t={k * 1.2}&w=100&h=700", content=jpeg(), headers=H)
        handled, inventory = 0, None
        while handled < 3:                                         # each "frame" event covers itself + any skipped ones
            m = ws.receive_json()
            if m["type"] == "inventory":
                inventory = m
            elif m["type"] == "frame":
                handled += 1 + m.get("skipped", 0)
        while True:                                                # the inventory that follows the last frame event
            m = ws.receive_json()
            if m["type"] == "inventory":
                inventory = m
                break
    assert inventory["counts"]["books"] == 1 and inventory["counts"]["analysed"] >= 1


def test_the_phone_saying_it_is_too_blurry_gets_a_spoken_and_shown_slow_down(client):
    sid = client.post("/api/sweeps", json={}, headers=H).json()["sweep_id"]
    with client.websocket_connect(f"/ws/live?sweep={sid}&t={TOK}") as phone:
        phone.receive_json(); phone.receive_json()                  # phone presence + first inventory
        phone.send_text('{"type": "blurry", "n": 3}')
        m = phone.receive_json()
        assert m["type"] == "coach" and m["key"] == "slow" and "Slow down" in m["ask"]
        phone.send_text('{"type": "blurry", "n": 4}')               # again straight away: held back
        phone.send_text("not json")                                 # garbage never breaks the connection
        sess = client.app.state.sessions[sid]
        said = sess.inject.get_nowait()
        assert said.startswith('GUIDE ask="Slow down') and sess.inject.empty()


def test_the_area_done_message_is_sent_on_a_timer_even_when_no_frames_arrive(tmp_path):
    import time
    cfg = dataclasses.replace(CFG, sweeps_dir=tmp_path, keyframe_max_bytes=300_000, guide_tick_s=0.2, guide_idle_s=1.0)
    v = FakeVision([Detection((100, 50, 160, 450), "book", "upright")], reads=[SpineRead("Dune", "Frank Herbert", None, True)] * 4)
    app = create_app(TOK, cfg, vision=v, catalogue=FakeCatalogue({"dune": [cand("Dune", "Frank Herbert")]}), voice=FakeVoice())
    with TestClient(app) as c:
        sid = c.post("/api/sweeps", json={}, headers=H).json()["sweep_id"]
        with c.websocket_connect(f"/ws/live?sweep={sid}&t={TOK}"):                 # the phone is connected
            c.post(f"/api/sweeps/{sid}/frames?seq=0&t=0&w=100&h=700", content=jpeg(), headers=H)   # one frame, then silence
            sess, said = app.state.sessions[sid], []
            for _ in range(80):
                while not sess.inject.empty():
                    said.append(sess.inject.get_nowait())
                if any("looks done" in x for x in said):
                    break
                time.sleep(0.1)
    assert any(x.startswith('GUIDE ask="That area looks done') for x in said)


def test_setup_over_the_websocket_focus_then_room_then_sweep(tmp_path):
    cfg = dataclasses.replace(CFG, sweeps_dir=tmp_path, setup_first_s=0.0, guide_gap_s=0.0)
    app = create_app(TOK, cfg, vision=FakeVision([]), catalogue=FakeCatalogue({}), voice=FakeVoice())
    with TestClient(app) as c:
        sid = c.post("/api/sweeps", json={"country": "GB", "currency": "GBP"}, headers=H).json()["sweep_id"]
        s = None
        with c.websocket_connect(f"/ws/live?sweep={sid}&t={TOK}") as ws:
            def until(pred):
                while not pred(m := ws.receive_json()):
                    pass
                return m
            ws.send_json({"type": "setup", "step": "hello", "ar": True})
            assert until(lambda m: m["type"] == "coach")["key"] == "setup:focus"
            ws.send_json({"type": "setup", "step": "focused"})
            assert until(lambda m: m["type"] == "coach")["key"] == "setup:room"
            r = c.post(f"/api/sweeps/{sid}/room/corners", headers=H,
                       json={"corners": [[0, 0, 0], [4, 0, 0], [4, 0, 3], [0, 0, 3]], "ceiling_y": 2.5})
            assert r.status_code == 200
            m = until(lambda m: m["type"] == "coach")
            assert m["key"] == "setup:start" and "12 square metres" in m["ask"]
            s = app.state.sessions[sid]
        said = []
        while not s.inject.empty():
            said.append(s.inject.get_nowait())
        assert [x.split('"')[1][:20] for x in said] == [
            "First, let's get the", "Sharp, good. Now the", "Room measured: 12 sq"]


def test_no_country_or_currency_is_assumed_and_finish_waits_for_them(tmp_path):
    import json
    cfg = dataclasses.replace(CFG, sweeps_dir=tmp_path)
    app = create_app(TOK, cfg, vision=FakeVision([]), catalogue=FakeCatalogue({}), voice=FakeVoice())
    with TestClient(app) as c:
        sid = c.post("/api/sweeps", json={"country": None, "currency": ""}, headers=H).json()["sweep_id"]
        meta = json.loads((tmp_path / sid / "meta.json").read_text())
        assert meta["country"] is None and meta["currency"] is None
        c.post(f"/api/sweeps/{sid}/finish", headers=H)
        s = app.state.sessions[sid]
        assert not s.finished and "which country" in s.inject.get_nowait()          # asks instead of building
        s.hooks.on_locale("GB", "GBP")
        assert json.loads((tmp_path / sid / "meta.json").read_text())["currency"] == "GBP"
