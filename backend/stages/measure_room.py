"""MEASURE-ROOM stage: frames  ->  room.json

First choice: room corners measured in the browser with WebXR (room_corners.json, see stages/room_corners.py).
Otherwise the reconstruction path below.

  Reconstructor (adapter, GPU) -> unscaled points + up vector + marker side in model units
  -> scale = marker_side_cm / median(marker side in model units)
  -> floor / ceiling heights, floor polygon, wall area      (pure math below, unit-tested)

Without a reconstruction or a marker the room fields stay None (never 0) and assemble queues
`room_scale_missing`. Fallbacks from the design (floor-marker homography, user-stated height) are
NOT built yet; see README.

Floor polygon = convex hull of floor-level points; L-shaped rooms are therefore over-estimated
(noted in `notes`). Wall area is gross: doors/windows are not subtracted.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from backend import deps
from backend.core import store
from backend.config import CFG, Config
from backend.ports import Reconstruction, Reconstructor
from backend.schemas import Frame, Room
from backend.stages.room_corners import room_from_corners

FT2_PER_M2 = 10.7639


def room_from_points(points: np.ndarray, up: np.ndarray, scale: float, floor_band: float = 0.05) -> Room:
    """`points` unscaled, `scale` converts model units to metres. Pure function."""
    up = up / np.linalg.norm(up)
    # rotate so `up` becomes +z (Rodrigues via cv2)
    z = np.array([0.0, 0.0, 1.0])
    axis = np.cross(up, z)
    s = np.linalg.norm(axis)
    if s < 1e-9:
        R = np.eye(3) if up @ z > 0 else np.diag([1.0, -1.0, -1.0])
    else:
        R, _ = cv2.Rodrigues(axis / s * np.arctan2(s, up @ z))
    p = (points @ R.T) * scale
    zs = p[:, 2]
    floor_z, ceil_z = np.percentile(zs, 2), np.percentile(zs, 98)
    height = float(ceil_z - floor_z)
    floor_pts = p[zs < floor_z + max(floor_band * height, 0.02), :2].astype(np.float32)
    hull = cv2.convexHull(floor_pts)
    area = float(cv2.contourArea(hull))
    (_, _), (a, b), _ = cv2.minAreaRect(hull)
    perim = float(cv2.arcLength(hull, True))
    wall = perim * height
    r = Room(length_m=round(max(a, b), 2), width_m=round(min(a, b), 2), height_m=round(height, 2),
             floor_area_m2=round(area, 2), floor_area_ft2=round(area * FT2_PER_M2, 1),
             wall_area_m2=round(wall, 2), wall_area_ft2=round(wall * FT2_PER_M2, 1),
             notes=["floor polygon = convex hull of floor points (L-shaped rooms are over-estimated)",
                    "wall area is gross: openings not subtracted"])
    return r


def run(sd: Path, reconstructor: Reconstructor | None = None, cfg: Config = CFG) -> None:
    sd = Path(sd)
    corners = store.read(sd, "room_corners.json", dict, default=None)
    if corners and len(corners.get("corners", [])) >= 3:          # measured in the browser with WebXR: metric
        store.write(sd, "room.json", room_from_corners(corners["corners"], corners.get("ceiling_y")))
        return
    frames = store.read(sd, "frames.json", list[Frame], default=[])
    rec: Reconstruction | None = None
    try:
        rec = (reconstructor or deps.reconstructor()).reconstruct(sd, frames)
    except Exception as e:
        room = Room(notes=[f"reconstruction failed: {e}"])
    else:
        room = Room(notes=["no reconstruction available"])
    if rec is not None:
        sides = [x for x in rec.marker_sides_model if x > 0]
        if not (cfg.marker_side_cm and sides):
            room = Room(notes=["reconstruction has no marker for metric scale"])
        else:
            scale = (cfg.marker_side_cm / 100) / float(np.median(sides))
            spread = float(np.std(sides) / np.median(sides)) if len(sides) > 1 else 0.1
            room = room_from_points(rec.points, rec.up, scale)
            room.scale_method = f"aruco marker x{len(sides)} (side {cfg.marker_side_cm} cm)"
            room.confidence = round(max(0.0, 1 - 5 * spread), 2)
    store.write(sd, "room.json", room)
