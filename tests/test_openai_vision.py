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
