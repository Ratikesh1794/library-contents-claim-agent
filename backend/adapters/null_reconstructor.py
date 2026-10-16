"""Placeholder room reconstructor: returns None, so room fields stay None and the review queue
gets `room_scale_missing` (honest blank > invented number).

TO UPGRADE: write e.g. adapters/vggt_reconstructor.py with
    reconstruct(sweep_dir, frames) -> Reconstruction(points, up, marker_sides_model)
(run VGGT/COLMAP on ~60-100 evenly spaced frames, ideally on a GPU worker) and return it from
backend/deps.py:reconstructor(). measure_room.py already does plane fit, scaling and areas."""
from __future__ import annotations

from pathlib import Path

from backend.ports import Reconstruction
from backend.schemas import Frame


class NullReconstructor:
    def reconstruct(self, sweep_dir: Path, frames: list[Frame]) -> Reconstruction | None:
        return None
