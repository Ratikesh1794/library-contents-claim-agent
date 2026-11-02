"""MEASURE-ROOM stage: room_corners.json  ->  room.json

The room is measured in the browser with WebXR (room_corners.json, see stages/room_corners.py). Without those
corners the room fields stay None (never 0) and assemble queues `room_scale_missing`.
"""
from __future__ import annotations

from pathlib import Path

from backend.core import store
from backend.config import CFG, Config
from backend.schemas import Room
from backend.stages.room_corners import room_from_corners


def run(sd: Path, cfg: Config = CFG) -> None:
    sd = Path(sd)
    corners = store.read(sd, "room_corners.json", dict, default=None)
    if corners and len(corners.get("corners", [])) >= 3:
        room = room_from_corners(corners["corners"], corners.get("ceiling_y"))
    else:
        room = Room(notes=["room not measured (no WebXR corners)"])
    store.write(sd, "room.json", room)
