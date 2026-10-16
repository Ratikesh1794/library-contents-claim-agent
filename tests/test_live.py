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
        assert ws.receive_json()["type"] == "inventory"


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
