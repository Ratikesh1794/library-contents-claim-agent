"""IDENTIFY stage: frames.json + frames/*.jpg  ->  sightings.json

Per keyframe: segment -> quality gate -> occlusion check -> crop/upscale -> orient -> read -> match.
No-guess rules (enforced here and in catalogue.resolve):
  * title, author, edition and ISBN only ever come from a catalogue record the spine confirms (catalogue.resolve)
  * the raw read is stored in `raw_text` and never promoted
  * two rotations that accept different works => unidentified/low_confidence

Per-frame results are written to sightings/<frame_id>.json, so the live path can call
`identify_frame` as keyframes arrive and `run` later only does the frames still missing.

Speed/cost:
  * "Only analyse what is new" (carry_over): a frame that mostly shows the last analysed frame copies its results.
  * "Read once": in an analysed frame, a book box that tracks onto an already-identified box reuses that read.
  * Crops within a frame are read in parallel (crop_workers).
Not-a-book check: the spine-read call also says whether the crop is a book at all; non-books are dropped.

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
    """-> (Match|None, rotation, raw_text, legible, is_book). Tries rotations, keeps the best accepted.
    If the model says the crop is not a book at all (bottle, box, DVD...), stop and report is_book=False."""
    tried: list[tuple[cat.Match, int]] = []
    raw, legible = None, False
    for rot in _rotations(layout, bbox, cfg):
        img = crop if rot == 0 else cv2.rotate(crop, _ROT[rot])
        try:
            read = vision.read_spine(img)
        except Exception as e:
            log.warning("read failed: %s", e)
            continue
        if not read.is_book:
            return None, rot, None, False, False
        if read.legible:
            legible = True
            raw = raw or " | ".join(x for x in (read.title, read.author) if x) or None
        m = cat.resolve(read, catalogue, cfg)
        tried.append((m, rot))
        if m.candidate and cfg.orient_early_exit:
            break
    if not tried:
        return None, None, raw, legible, True
    accepted = [(m, r) for m, r in tried if m.candidate]
    if len({cat.work_key(m.candidate) for m, _ in accepted}) > 1:      # rotations disagree
        m, r = max(tried, key=lambda t: t[0].score)
        return cat.Match(None, 0.0, "low_confidence", m.candidates), r, raw, legible, True
    m, r = max(accepted or tried, key=lambda t: t[0].score)
    return m, r, raw, legible, True


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

    def one(i: int, d: Detection) -> Sighting | None:
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
                    m, rot, raw, legible, is_book = _read_book(crop, d.layout, d.bbox, vision, catalogue, cfg)
                    if not is_book:
                        log.info("dropped %s: the reader says it is not a book", s.id)
                        return None                  # not a book: never counted
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
        return [x for x in ex.map(one, range(len(dets)), dets) if x is not None]


def is_carried(sightings: list[Sighting]) -> bool:
    """True if these sightings were copied from an earlier frame (carry_over) rather than detected here."""
    return bool(sightings) and all(s.carried_from for s in sightings)


def _coverage(H: np.ndarray, anchor_shape, w: int, h: int) -> float:
    """Fraction of the new frame (w x h) that the anchor frame, projected through H, already covered."""
    ah, aw = anchor_shape[:2]
    quad = cv2.perspectiveTransform(np.float32([[[0, 0]], [[aw, 0]], [[aw, ah]], [[0, ah]]]), H).reshape(-1, 2)
    rect = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    area, _ = cv2.intersectConvexConvex(quad, rect)
    return float(area) / (w * h)


def carry_over(sd: Path, frame: Frame, anchor: tuple[Frame, list[Sighting]] | None,
               cfg: Config = CFG) -> list[Sighting] | None:
    """"Only analyse what is new." If `frame` mostly shows what `anchor` (the last frame that was actually
    ANALYSED) already showed, return the anchor's sightings moved into this frame, with no model calls.
    Returns None when the frame must be analysed:
      * no anchor, or the anchor found nothing (keeps "carried" unambiguous: carried lists are never empty)
      * the anchor had fixable failures (blur, glare, too far...): the user may be fixing them right now
      * the frames don't align, or less than novelty_skip_coverage of this frame was already seen
      * the camera moved closer (things > novelty_max_zoom bigger): more detail may now be readable
    Always compare with the last ANALYSED frame, never the previous carried one, or a slow pan would drift
    forever without looking at the books entering the edge of the view.
    """
    if anchor is None:
        return None
    a_frame, a_sightings = anchor
    if not a_sightings or any(s.reason in cfg.retry_reasons for s in a_sightings if not s.accepted):
        return None
    try:
        img_a, img_b = store.load_image(sd, a_frame.path), store.load_image(sd, frame.path)
    except FileNotFoundError:
        return None
    H = track.homography(img_a, img_b, cfg)
    if H is None or float(np.sqrt(abs(np.linalg.det(H[:2, :2])))) > cfg.novelty_max_zoom:
        return None
    h, w = img_b.shape[:2]
    if _coverage(H, img_a.shape, w, h) < cfg.novelty_skip_coverage:
        return None
    out: list[Sighting] = []
    for s in a_sightings:
        p = track.project(H, s.bbox)
        c = (max(0, p[0]), max(0, p[1]), min(w, p[2]), min(h, p[3]))
        if c[2] <= c[0] or c[3] <= c[1] or track.area(c) < 0.5 * track.area(p):
            continue                                     # mostly out of view now
        out.append(s.model_copy(update={"id": f"s_{frame.id[2:]}_c{len(out):02d}", "frame_id": frame.id,
                                        "bbox": c, "carried_from": s.carried_from or s.id}))
    books = [s for s in out if s.kind == "book"]
    for s, r in zip(books, assign_rows([b.bbox for b in books])):
        s.shelf_hint = f"row{r + 1}"
    return out or None


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
    """Identify every frame that has no sightings yet, in time order, carrying results over from the last
    analysed frame where the view is already covered (same rule as the live path)."""
    sd = Path(sd)
    vision, catalogue = vision or deps.vision(), catalogue or deps.catalogue()
    frames = sorted(store.read(sd, "frames.json", list[Frame], default=[]), key=lambda f: f.t)
    (sd / "sightings").mkdir(exist_ok=True)
    anchor: tuple[Frame, list[Sighting]] | None = None
    ok = True
    for f in frames:
        path = sd / "sightings" / f"{f.id}.json"
        if path.exists():                            # done live: just keep track of the last analysed frame
            done = store.read(sd, f"sightings/{f.id}.json", list[Sighting], default=[])
            if not is_carried(done):
                anchor = (f, done)
            continue
        if deadline and time.time() > deadline:
            ok = False
            continue
        try:
            sightings = carry_over(sd, f, anchor, cfg)
            if sightings is None:
                sightings = identify_frame(sd, f, vision, catalogue, cfg, prev=anchor)
                anchor = (f, sightings)
            store.write(sd, f"sightings/{f.id}.json", sightings)
        except Exception as e:                       # NFR-E5: one bad frame must not kill the stage
            log.warning("frame %s failed: %s", f.id, e)
            ok = False
    all_s: list[Sighting] = []
    for f in frames:
        all_s += store.read(sd, f"sightings/{f.id}.json", list[Sighting], default=[])
    store.write(sd, "sightings.json", all_s)
    (store.clear_incomplete if ok else store.mark_incomplete)(sd, "identify")
