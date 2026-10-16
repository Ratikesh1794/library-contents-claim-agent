"""MEASURE-SCALE stage: frames  ->  geometry.json {scale: {frame_id: [MarkerObs]}}

A printed ArUco marker (side = cfg.marker_side_cm, MEASURE IT WITH A RULER) on the shelf front gives
px/cm in each frame where it is visible. We store every marker seen; measure_books picks the one
nearest the book (closest ~ most likely coplanar).

Rejected: markers whose opposite sides differ by more than marker_max_skew (steep viewing angle).
ponytail: scalar px/cm only. Upgrade = rectify the shelf plane with the marker homography
(cv2.getPerspectiveTransform on the 4 corners) and measure in the rectified image.
Optional lens correction: put camera.json {"camera_matrix": 3x3, "dist_coeffs": [...]} in the sweep dir.
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from backend.core import store
from backend.config import CFG, Config
from backend.schemas import Frame, Geometry, MarkerObs


def detect_markers(img: np.ndarray, cfg: Config = CFG) -> list[MarkerObs]:
    if not cfg.marker_side_cm:
        return []
    det = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, cfg.marker_dict)),
                                  cv2.aruco.DetectorParameters())
    corners, ids, _ = det.detectMarkers(img)
    out = []
    for c, mid in zip(corners, ids.flatten() if ids is not None else []):
        p = c.reshape(4, 2)
        sides = [float(np.linalg.norm(p[i] - p[(i + 1) % 4])) for i in range(4)]
        skew = max(sides[0] / sides[2], sides[2] / sides[0], sides[1] / sides[3], sides[3] / sides[1])
        if skew > cfg.marker_max_skew:
            continue
        side = float(np.mean(sides))
        cx, cy = p.mean(axis=0)
        out.append(MarkerObs(marker_id=int(mid), center=(float(cx), float(cy)), side_px=side,
                             px_per_cm=side / cfg.marker_side_cm, skew=skew))
    return out


def nearest_marker(markers: list[MarkerObs], bbox) -> MarkerObs | None:
    if not markers:
        return None
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    return min(markers, key=lambda m: (m.center[0] - cx) ** 2 + (m.center[1] - cy) ** 2)


def run(sd: Path, cfg: Config = CFG) -> None:
    sd = Path(sd)
    frames = store.read(sd, "frames.json", list[Frame], default=[])
    geo = Geometry()
    if not cfg.marker_side_cm:
        geo.notes.append("MARKER_SIDE_CM unset: no scale, all dimensions will be blank")
    cam = sd / "camera.json"
    K = D = None
    if cam.exists():
        d = json.loads(cam.read_text())
        K, D = np.array(d["camera_matrix"], float), np.array(d["dist_coeffs"], float)
    else:
        geo.notes.append("no camera.json: lens distortion not corrected (lower scale confidence)")
    for f in frames:
        img = store.load_image(sd, f.path)
        if K is not None:
            img = cv2.undistort(img, K, D)
        found = detect_markers(img, cfg)
        if found:
            geo.scale[f.id] = found
    store.write(sd, "geometry.json", geo)
