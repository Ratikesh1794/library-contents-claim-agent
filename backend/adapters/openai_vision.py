"""Vision via OpenAI: segment frames, read one spine crop, describe one item.
Structured outputs (schema-constrained JSON); every response cached by (model, prompt, image hash).
Prompts: backend/prompts/vision.py (DETECT, READ_SPINE, DESCRIBE_ITEM).
Key: OPENAI_API_KEY. Models: VISION_MODEL (see config.py).

KNOWN WEAK POINT: general OpenAI vision models read text well but are NOT reliable at drawing
bounding boxes. `detect` asks for pixel boxes and sanity-filters them, but expect loose or missing
boxes on dense shelves. The fix is a swap, not a rewrite: implement `detect` with a local detector
(Grounding DINO / OWLv2 / YOLO-World + SAM) in a new adapter and keep this class for read_spine and
describe_item (compose them in deps.vision()).

TO UPGRADE: change VISION_MODEL, or implement the `Vision` protocol (ports.py) with another provider.
"""
from __future__ import annotations

import base64
import hashlib
from typing import Literal

import cv2
import numpy as np
from pydantic import BaseModel

from backend.core import meter
from backend.core.cache import cached
from backend.config import CFG, Config
from backend.prompts import fill, vision as prompts
from backend.ports import Detection, SpineRead
from backend.schemas import ItemGuess

MAX_SIDE = 1568   # larger frames are downscaled for detection; boxes are scaled back


class _Det(BaseModel):
    box: list[int]
    kind: Literal["book", "item"]
    layout: Literal["upright", "flat", "stacked", "cover_only", "unknown"] | None = None


class _Dets(BaseModel):
    detections: list[_Det]


class _Read(BaseModel):
    is_book: bool = True
    title: str | None = None
    author: str | None = None
    publisher: str | None = None
    legible: bool = False


class _Item(BaseModel):
    name: str | None = None
    category: str | None = None
    material: str | None = None
    brand_model: str | None = None
    dims_hint: str | None = None


class OpenAIVision:
    def __init__(self, cfg: Config = CFG, client=None):
        self.cfg, self._client = cfg, client

    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI()          # reads OPENAI_API_KEY
        return self._client

    def _ask(self, name: str, template: str, img: np.ndarray, schema: type[BaseModel], max_side: int | None = None, **values):
        """Fill the prompt for this image and ask. -> (parsed, scale = sent_size / original_size)."""
        scale = 1.0
        if max_side and max(img.shape[:2]) > max_side:
            scale = max_side / max(img.shape[:2])
            img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        h, w = img.shape[:2]
        prompt = fill(template, width=w, height=h, **values)
        _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
        jpg = buf.tobytes()
        extra = {} if self.cfg.vision_model.startswith(("gpt-5", "o1", "o3", "o4")) else {"temperature": 0}

        def call():
            r = self.client.chat.completions.parse(
                model=self.cfg.vision_model, response_format=schema, **extra,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpg).decode(),
                                                        "detail": "high"}}]}])
            tok = getattr(r.usage, "total_tokens", 0) or 0
            meter.add("vision", tokens=tok, usd=tok / 1e6 * self.cfg.usd_per_mtok)
            msg = r.choices[0].message
            if getattr(msg, "refusal", None) or msg.parsed is None:
                raise ValueError("vision model gave no parsable answer")
            return msg.parsed.model_dump_json()

        text, _ = cached("vision", [self.cfg.vision_model, name, prompt, hashlib.sha256(jpg).hexdigest()],
                         call, self.cfg.cache_dir)
        return schema.model_validate_json(text), scale

    def detect(self, img: np.ndarray) -> list[Detection]:
        H, W = img.shape[:2]
        res, scale = self._ask("detect", prompts.DETECT, img, _Dets, MAX_SIDE)
        out = []
        for d in res.detections:
            if len(d.box) != 4:
                continue
            x0, y0, x1, y1 = (int(v / scale) for v in d.box)
            x0, x1 = sorted((max(0, x0), min(W, x1)))
            y0, y1 = sorted((max(0, y0), min(H, y1)))
            if x1 > x0 and y1 > y0:
                out.append(Detection((x0, y0, x1, y1), d.kind, d.layout))
        return out

    def read_spine(self, crop: np.ndarray) -> SpineRead:
        r, _ = self._ask("read", prompts.READ_SPINE, crop, _Read)
        return SpineRead(r.title, r.author, r.publisher, r.legible, r.is_book)

    def describe_item(self, crop: np.ndarray) -> ItemGuess:
        r, _ = self._ask("item", prompts.DESCRIBE_ITEM, crop, _Item, categories=", ".join(self.cfg.item_categories))
        cat = r.category if r.category in self.cfg.item_categories else "other" if r.category else None
        return ItemGuess(name=r.name, category=cat, material=r.material, brand_model=r.brand_model, dims_hint=r.dims_hint)
