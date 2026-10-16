"""Frame-to-frame tracking. Lets us say "this box in frame B is the same physical object as that box in frame A".

How: overlapping keyframes are aligned with a homography (ORB features + RANSAC, OpenCV). A box from frame A is
projected into frame B; if it lands on a box of the same kind there (IoU >= track_iou), it is the same object.
A homography is exact for a flat scene (a shelf front) and close enough for a slow pan; items at other depths
are matched more loosely, which is why the IoU bar is modest.

Used by dedupe (one object per physical book, read or not) and identify (reuse an earlier read instead of
paying for the same spine again). Homographies are cached per sweep in links.json, so re-runs are free.
ponytail: pairwise links only (frame i -> i+1..i+track_span). Coming back to a shelf much later is handled
by the title rule in dedupe, not by tracking; a full SfM/loop-closure would be the upgrade.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import cv2
import numpy as np

from backend.core import store
from backend.config import CFG, Config
from backend.schemas import BBox, Frame

_lock = threading.Lock()          # links.json is written from the live worker and the pipeline


def _features(img: np.ndarray, cfg: Config):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    s = min(1.0, cfg.track_max_side / max(gray.shape[:2]))
    small = cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else gray
    kp, desc = cv2.ORB_create(cfg.track_features).detectAndCompute(small, None)
    return kp, desc, s


def homography(img_a: np.ndarray, img_b: np.ndarray, cfg: Config = CFG) -> np.ndarray | None:
    """3x3 matrix mapping frame-A pixels to frame-B pixels (full resolution), or None if they don't overlap."""
    ka, da, sa = _features(img_a, cfg)
    kb, db, sb = _features(img_b, cfg)
    if da is None or db is None or len(ka) < cfg.track_min_inliers or len(kb) < cfg.track_min_inliers:
        return None
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da, db, k=2)
    good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
    if len(good) < cfg.track_min_inliers:
        return None
    src = np.float32([ka[m.queryIdx].pt for m in good]) / sa          # back to full-resolution pixels
    dst = np.float32([kb[m.trainIdx].pt for m in good]) / sb
    H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 6.0)
    if H is None or int(mask.sum()) < cfg.track_min_inliers:
        return None
    scale = abs(np.linalg.det(H[:2, :2]))
    if not 0.25 < scale < 4.0:                                        # degenerate warp: don't trust it
        return None
    return H


def project(H: np.ndarray, box: BBox) -> BBox:
    x0, y0, x1, y1 = box
    pts = cv2.perspectiveTransform(np.float32([[[x0, y0]], [[x1, y0]], [[x1, y1]], [[x0, y1]]]), H).reshape(-1, 2)
    return (int(pts[:, 0].min()), int(pts[:, 1].min()), int(pts[:, 0].max()), int(pts[:, 1].max()))


def iou(a: BBox, b: BBox) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _key(a: str, b: str) -> str:
    return f"{a}>{b}"


def links(sd: Path, frames: list[Frame], cfg: Config = CFG) -> dict[tuple[str, str], np.ndarray | None]:
    """Homographies between each frame and the next `track_span` frames (by time). Computes only missing pairs."""
    sd = Path(sd)
    order = sorted(frames, key=lambda f: f.t)
    want = [(order[i], order[j]) for i in range(len(order)) for j in range(i + 1, min(len(order), i + 1 + cfg.track_span))]
    with _lock:
        cache: dict = json.loads((sd / "links.json").read_text()) if (sd / "links.json").exists() else {}
    imgs: dict[str, np.ndarray] = {}

    def img(f: Frame):
        if f.id not in imgs:
            imgs[f.id] = store.load_image(sd, f.path)
        return imgs[f.id]

    added = False
    for a, b in want:
        k = _key(a.id, b.id)
        if k not in cache:
            try:
                H = homography(img(a), img(b), cfg)
            except FileNotFoundError:
                H = None
            cache[k] = None if H is None else H.tolist()
            added = True
    if added:
        with _lock:
            on_disk = json.loads((sd / "links.json").read_text()) if (sd / "links.json").exists() else {}
            store.write(sd, "links.json", {**on_disk, **cache})
    return {(a.id, b.id): (None if cache[_key(a.id, b.id)] is None else np.array(cache[_key(a.id, b.id)]))
            for a, b in want}
