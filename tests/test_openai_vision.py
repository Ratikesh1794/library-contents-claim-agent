import dataclasses
from types import SimpleNamespace

import numpy as np

from backend.adapters import openai_vision as ov
from backend.config import CFG


class FakeClient:
    """Mimics client.chat.completions.parse(...) and records the calls."""
    def __init__(self, parsed):
        self.parsed, self.calls = parsed, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(parse=self._parse))

    def _parse(self, **kw):
        self.calls.append(kw)
        msg = SimpleNamespace(parsed=self.parsed, refusal=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=SimpleNamespace(total_tokens=100))


def vision(tmp_path, parsed, model="gpt-4.1"):
    cfg = dataclasses.replace(CFG, cache_dir=tmp_path / "cache", vision_model=model)
    c = FakeClient(parsed)
    return ov.OpenAIVision(cfg, c), c


def test_detect_scales_boxes_back_and_clips(tmp_path):
    dets = ov._Dets(detections=[ov._Det(box=[100, 100, 200, 400], kind="book", layout="upright"),
                                ov._Det(box=[10, 10, 5, 5], kind="item"),                 # reversed corners: sorted
                                ov._Det(box=[1, 2, 3], kind="book")])                     # malformed: dropped
    v, c = vision(tmp_path, dets)
    img = np.zeros((2000, 3136, 3), np.uint8)          # larger than MAX_SIDE => downscaled by 0.5, boxes x2
    out = v.detect(img)
    assert out[0].bbox == (200, 200, 400, 800) and out[0].layout == "upright"
    assert out[1].bbox == (10, 10, 20, 20) or len(out) == 2
    assert len(out) == 2
    assert "1568x1000 pixels" in c.calls[0]["messages"][0]["content"][0]["text"]


def test_read_spine_item_and_cache(tmp_path):
    v, c = vision(tmp_path, ov._Read(title="Dune", author="Frank Herbert", publisher=None, legible=True))
    crop = np.full((50, 20, 3), 7, np.uint8)
    r = v.read_spine(crop)
    assert (r.title, r.author, r.legible) == ("Dune", "Frank Herbert", True)
    v.read_spine(crop)
    assert len(c.calls) == 1                           # second identical call came from the cache
    v2, _ = vision(tmp_path / "x", ov._Item(category="chandelier", material="brass"))
    assert v2.describe_item(crop).category == "other"  # unknown categories collapse to "other"


def test_temperature_only_for_non_reasoning_models(tmp_path):
    crop = np.full((50, 20, 3), 9, np.uint8)
    v, c = vision(tmp_path / "a", ov._Read(legible=False), "gpt-4.1"); v.read_spine(crop)
    assert c.calls[0]["temperature"] == 0
    v, c = vision(tmp_path / "b", ov._Read(legible=False), "gpt-5"); v.read_spine(crop)
    assert "temperature" not in c.calls[0]
