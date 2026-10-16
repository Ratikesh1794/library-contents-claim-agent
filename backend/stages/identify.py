"""IDENTIFY stage: frames.json + frames/*.jpg  ->  sightings.json

Per keyframe: segment -> quality gate -> occlusion check -> crop/upscale -> orient -> read -> match.
No-guess rules (enforced here and in catalogue.resolve):
  * a title only ever comes from a catalogue Candidate that passed accept_T
  * the raw read is stored in `raw_text` and never promoted
  * two rotations that accept different works => unidentified/low_confidence

Per-frame results are written to sightings/<frame_id>.json, so the live path can call
`identify_frame` as keyframes arrive and `run` later only does the frames still missing.

Speed/cost: crops within a frame are read in parallel (crop_workers). When the previous frame is given,
a book box that tracks onto an already-identified box (backend/stages/track.py, IoU >= reuse_iou) reuses that
read instead of calling the vision model again ("read once").

TO UPGRADE: swap the Vision / Catalogue adapters (backend/deps.py); add rectification with the
marker homography at the "rectify" comment.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np

from backend.stages import catalogue as cat
from backend import deps
from backend.core import store
from backend.stages import quality, track
from backend.config import CFG, Config
from backend.stages.dedupe import assign_rows
from backend.ports import Catalogue, Detection, Vision
from backend.schemas import Frame, Sighting

log = logging.getLogger(__name__)
_ROT = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}


def _iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union else 0.0


def _valid(d: Detection, cfg: Config) -> bool:
    x0, y0, x1, y1 = d.bbox
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0 or w * h < cfg.min_box_px:
        return False
    return not (d.kind == "book" and max(w, h) / min(w, h) > cfg.max_spine_aspect)


def _crop(img: np.ndarray, bbox, margin: float) -> np.ndarray:
    x0, y0, x1, y1 = bbox
    mx, my = int((x1 - x0) * margin), int((y1 - y0) * margin)
    H, W = img.shape[:2]
    return img[max(0, y0 - my):min(H, y1 + my), max(0, x0 - mx):min(W, x1 + mx)]


def _upscale(crop: np.ndarray, cfg: Config) -> np.ndarray:
    text_h = 0.6 * min(crop.shape[:2])           # proxy: text height ~ 60% of spine thickness
    if text_h >= cfg.target_text_px or text_h <= 0:
        return crop
    f = min(cfg.max_upscale, cfg.target_text_px / text_h)
    return cv2.resize(crop, None, fx=f, fy=f, interpolation=cv2.INTER_LANCZOS4)


def _rotations(layout: str | None, bbox, cfg: Config) -> list[int]:
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if layout == "upright" and h / max(w, 1) > 2.5:
        return [90, 270]
    if layout == "flat":
        return [0, 180]
    return [0, 90, 180, 270]


def _read_book(crop: np.ndarray, layout, bbox, vision: Vision, catalogue: Catalogue, cfg: Config):
    """-> (Match|None, rotation, raw_text, legible). Tries rotations, keeps the best accepted."""
    tried: list[tuple[cat.Match, int]] = []
    raw, legible = None, False
    for rot in _rotations(layout, bbox, cfg):
        img = crop if rot == 0 else cv2.rotate(crop, _ROT[rot])
        try:
            read = vision.read_spine(img)
        except Exception as e:
            log.warning("read failed: %s", e)
            continue
        if read.legible:
            legible = True
            raw = raw or " | ".join(x for x in (read.title, read.author) if x) or None
        m = cat.resolve(read, catalogue, cfg)
        tried.append((m, rot))
        if m.candidate and cfg.orient_early_exit:
            break
    if not tried:
        return None, None, raw, legible
    accepted = [(m, r) for m, r in tried if m.candidate]
    if len({cat.work_key(m.candidate) for m, _ in accepted}) > 1:      # rotations disagree
        m, r = max(tried, key=lambda t: t[0].score)
        return cat.Match(None, 0.0, "low_confidence", m.candidates), r, raw, legible
    m, r = max(accepted or tried, key=lambda t: t[0].score)
    return m, r, raw, legible


def identify_frame(sd: Path, frame: Frame, vision: Vision, catalogue: Catalogue,
                   cfg: Config = CFG, prev: tuple[Frame, list[Sighting]] | None = None) -> list[Sighting]:
    """`prev` = (previous frame, its sightings): enables "read once" reuse for books already identified."""
    img = store.load_image(sd, frame.path)
    H, W = img.shape[:2]
    dets = [d for d in vision.detect(img) if _valid(d, cfg)]
    books = [d for d in dets if d.kind == "book"]
    rows = assign_rows([d.bbox for d in books])
    row_of = {id(d): r for d, r in zip(books, rows)}
    reuse = _reusable(sd, frame, img, prev, cfg)

    def one(i: int, d: Detection) -> Sighting:
        s = Sighting(id=f"s_{frame.id[2:]}_{i:02d}", frame_id=frame.id, kind=d.kind, bbox=d.bbox,
                     layout=d.layout if d.kind == "book" else None,
                     shelf_hint=f"row{row_of[id(d)] + 1}" if d.kind == "book" else None)
        crop = _crop(img, d.bbox, cfg.crop_margin)
        q = quality.score(crop, cfg)
        s.crop_blur = q["blur"]
        x0, y0, x1, y1 = d.bbox
        if d.kind == "book":                         # read once: same book already identified in the previous frame
            hit = max(((track.iou(box, d.bbox), ps) for box, ps in reuse), default=(0, None), key=lambda t: t[0])
            if hit[0] >= cfg.reuse_iou:
                ps = hit[1]
                s.accepted, s.candidates, s.score = ps.accepted, ps.candidates, ps.score
                s.raw_text, s.legible, s.rotation_deg = ps.raw_text, ps.legible, ps.rotation_deg
                return s
        # Hard gates first: they skip the (paid) read entirely and feed the Coach.
        if d.kind == "book" and 0.6 * min(x1 - x0, y1 - y0) < cfg.min_text_px:
            s.reason = "too_far"
        else:
            s.reason = quality.failing_reason(q, cfg)
        # Soft reasons: kept only if the book ends up unidentified.
        soft = None
        if d.kind == "book":
            if any(o.kind == "item" and _iou(d.bbox, o.bbox) > cfg.occl_iou for o in dets):
                soft = "blocked"
            elif x0 <= 1 or y0 <= 1 or x1 >= W - 1 or y1 >= H - 1:
                soft = "partial"
            if d.layout == "cover_only":
                soft = "stack_hidden"
        if s.reason is None:
            crop = _upscale(crop, cfg)       # rectify with marker homography would go here
            try:
                if d.kind == "item":
                    s.item = vision.describe_item(crop)
                elif d.layout == "cover_only":
                    s.reason = "stack_hidden"
                else:
                    m, rot, raw, legible = _read_book(crop, d.layout, d.bbox, vision, catalogue, cfg)
                    s.raw_text, s.legible, s.rotation_deg = raw, legible, rot
                    if m is not None:
                        s.candidates, s.score = m.candidates[:5], m.score
                        s.accepted = m.candidate
                        s.reason = None if m.candidate else (soft or m.reason)
            except Exception as e:
                log.warning("identify crop failed: %s", e)
                s.reason = soft or "low_confidence"
        return s

    with ThreadPoolExecutor(max(1, cfg.crop_workers)) as ex:      # reads in parallel; order is kept
        return list(ex.map(one, range(len(dets)), dets))


def _reusable(sd: Path, frame: Frame, img: np.ndarray, prev, cfg: Config) -> list[tuple]:
    """Identified boxes from the previous frame, projected into this frame: [(box_here, sighting), ...]."""
    if not prev:
        return []
    prev_frame, prev_sightings = prev
    known = [p for p in prev_sightings if p.kind == "book" and p.accepted]
    if not known:
        return []
    try:
        H = track.homography(store.load_image(sd, prev_frame.path), img, cfg)
    except FileNotFoundError:
        return []
    return [] if H is None else [(track.project(H, p.bbox), p) for p in known]


def run(sd: Path, vision: Vision | None = None, catalogue: Catalogue | None = None,
        cfg: Config = CFG, deadline: float | None = None) -> None:
    sd = Path(sd)
    vision, catalogue = vision or deps.vision(), catalogue or deps.catalogue()
    frames = store.read(sd, "frames.json", list[Frame], default=[])
    (sd / "sightings").mkdir(exist_ok=True)
    todo = [f for f in frames if not (sd / "sightings" / f"{f.id}.json").exists()]

    def work(f: Frame):
        if deadline and time.time() > deadline:
            return False
        try:
            store.write(sd, f"sightings/{f.id}.json", identify_frame(sd, f, vision, catalogue, cfg))
        except Exception as e:                      # NFR-E5: one bad frame must not kill the stage
            log.warning("frame %s failed: %s", f.id, e)
            return False
        return True

    with ThreadPoolExecutor(cfg.identify_workers) as ex:
        ok = list(ex.map(work, todo))
    all_s: list[Sighting] = []
    for f in frames:
        all_s += store.read(sd, f"sightings/{f.id}.json", list[Sighting], default=[])
    store.write(sd, "sightings.json", all_s)
    if not all(ok):
        store.mark_incomplete(sd, "identify")
    else:
        store.clear_incomplete(sd, "identify")
