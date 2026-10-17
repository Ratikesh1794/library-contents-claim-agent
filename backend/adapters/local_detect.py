"""Object detection on this laptop, no prompt and no API cost (config: DETECTOR=local, the default).

Two open-source segmentation models (Ultralytics, AGPL-3.0: fine for personal use), compared on real sweep frames:
  * books: COCO YOLO11-seg, class "book". It splits packed and stacked books into one box each.
  * items: YOLOE-seg (open vocabulary), told what to look for by name (config.item_vocab). The vocabulary is baked
    into models/ once, so the 600 MB text encoder is only needed when item_vocab changes.
Each object's outline (mask) becomes a rotated rectangle (`quad`), so identify can straighten a tilted spine before
cropping. Duplicates inside one frame (a book found twice, a chair split into pieces) are removed here.
Reading spines and describing items still go to the vision model (`reader`).
"""
from __future__ import annotations

import hashlib
import logging
import threading

import cv2
import numpy as np

from backend.config import CFG, Config
from backend.ports import Detection, SpineRead, Vision
from backend.schemas import ItemGuess

log = logging.getLogger(__name__)
COCO_BOOK, COCO_PERSON = 73, 0


def _area(b) -> float:
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def _inter(a, b) -> float:
    return _area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def dedupe(dets: list[Detection], iou_t: float, ioa_t: float) -> list[Detection]:
    """One detection per object, per kind. Boxes are taken smallest first:
      * near-identical to a kept box (IoU)          -> the same object found twice: keep the more confident
      * contains exactly one kept box (IoA)         -> that box was a fragment: keep the whole object instead
      * contains two or more kept boxes             -> a group box (e.g. three stacked books as one): drop it"""
    kept: list[Detection] = []
    for d in sorted(dets, key=lambda d: _area(d.bbox)):
        same = [k for k in kept if k.kind == d.kind]
        twin = next((k for k in same if _inter(d.bbox, k.bbox) / ((_area(d.bbox) + _area(k.bbox) - _inter(d.bbox, k.bbox)) or 1) >= iou_t), None)
        if twin is not None:
            if d.conf > twin.conf:
                kept[kept.index(twin)] = d
            continue
        inside = [k for k in same if _inter(d.bbox, k.bbox) / (_area(k.bbox) or 1) >= ioa_t]
        if len(inside) >= 2:
            continue
        if inside:
            kept.remove(inside[0])
        kept.append(d)
    return kept


def drop_people(dets: list[Detection], persons: list[Detection], shape, overlap: float) -> list[Detection]:
    """Remove every detection mostly covered by a person's OUTLINE (not their bounding box: a person standing in front of
    a shelf must not hide the books beside them). The brief: do not capture people."""
    polys = [p.poly for p in persons if p.poly is not None and len(p.poly) >= 3]
    if not polys:
        return dets
    mask = np.zeros(shape[:2], np.uint8)
    cv2.fillPoly(mask, [np.asarray(q, np.int32) for q in polys], 1)
    keep = []
    for d in dets:
        x0, y0, x1, y1 = (max(0, v) for v in d.bbox)
        area = max(1, (x1 - x0) * (y1 - y0))
        if float(mask[y0:y1, x0:x1].sum()) / area < overlap:
            keep.append(d)
    return keep


def quad_of(poly: np.ndarray) -> tuple[list[tuple[float, float]], str]:
    """Outline polygon -> (rotated-rectangle corners, 'upright' if its long side is more vertical else 'flat')."""
    (cx, cy), (w, h), ang = cv2.minAreaRect(poly.astype(np.float32))
    pts = cv2.boxPoints(((cx, cy), (w, h), ang))
    e1, e2 = pts[1] - pts[0], pts[2] - pts[1]
    long_edge = e1 if np.hypot(*e1) >= np.hypot(*e2) else e2
    layout = "upright" if abs(long_edge[1]) >= abs(long_edge[0]) else "flat"
    return [(float(x), float(y)) for x, y in pts], layout


class LocalDetector:
    def __init__(self, cfg: Config = CFG):
        self.cfg, self._lock, self._models = cfg, threading.Lock(), None

    def _load(self):
        import torch
        from ultralytics import YOLO, YOLOE
        c = self.cfg
        c.models_dir.mkdir(parents=True, exist_ok=True)
        books = YOLO(str(c.models_dir / c.book_model))
        tag = hashlib.sha1("|".join(c.item_vocab).encode()).hexdigest()[:8]
        baked = c.models_dir / f"{c.item_model.removesuffix('.pt')}-vocab-{tag}.pt"
        if not baked.exists():                     # one-off: needs the text encoder (downloaded into models/)
            log.info("baking the item vocabulary into %s", baked.name)
            import os
            cwd = os.getcwd()
            os.chdir(c.models_dir)                 # Ultralytics fetches the text encoder into the working directory
            try:
                m = YOLOE(c.item_model)
                m.set_classes(list(c.item_vocab))
                m.save(baked.name)
            finally:
                os.chdir(cwd)
        items = YOLOE(str(baked))
        dev = "mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu"
        self._models = (books, items, dev)

    def _run(self, model, img, kind: str, **kw) -> list[Detection]:
        r = model.predict(img, conf=self.cfg.detect_conf, imgsz=self.cfg.detect_imgsz, device=self._models[2],
                          verbose=False, **kw)[0]
        polys = r.masks.xy if r.masks is not None else [None] * len(r.boxes)
        out = []
        for box, cls, conf, poly in zip(r.boxes.xyxy.tolist(), r.boxes.cls.tolist(), r.boxes.conf.tolist(), polys):
            quad, layout = quad_of(poly) if poly is not None and len(poly) >= 3 else (None, None)
            out.append(Detection(tuple(int(round(v)) for v in box), kind, layout if kind == "book" else None,
                                 quad, r.names[int(cls)], float(conf), poly))
        return out

    def detect(self, img: np.ndarray) -> list[Detection]:
        with self._lock:                           # one predict at a time (live worker + pipeline share the models)
            if self._models is None:
                self._load()
            books, items, _ = self._models
            first = self._run(books, img, "book", classes=[COCO_BOOK, COCO_PERSON])     # one pass finds books and people
            dets = [d for d in first if d.label != "person"] + self._run(items, img, "item")
            persons = [d for d in first if d.label == "person" and d.conf >= self.cfg.person_conf]
        dets = drop_people(dets, persons, img.shape, self.cfg.person_overlap)
        return dedupe(dets, self.cfg.detect_dup_iou, self.cfg.detect_dup_ioa)


class LocalDetectVision:
    """Vision with detection done locally; reading and describing delegated to `reader` (the vision model)."""

    def __init__(self, detector: LocalDetector, reader: Vision):
        self.detector, self.reader = detector, reader

    def detect(self, img: np.ndarray) -> list[Detection]:
        return self.detector.detect(img)

    def read_spine(self, crop: np.ndarray) -> SpineRead:
        return self.reader.read_spine(crop)

    def describe_item(self, crop: np.ndarray) -> ItemGuess:
        return self.reader.describe_item(crop)
