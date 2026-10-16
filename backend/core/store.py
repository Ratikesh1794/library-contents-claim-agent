"""Sweep directory I/O. A sweep is a folder of JSON + frames (design decision D8):

  sweeps/<id>/ meta.json frames.json frames/*.jpg user_facts.json sightings.json
               objects.json geometry.json room.json prices.json claim_packet.json
               report.html metrics.json incomplete.json

Stages only talk to each other through these files, so any stage can be re-run alone.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from pydantic import TypeAdapter
from pydantic_core import to_jsonable_python

from backend.config import CFG

SWEEP_ID_RE = re.compile(r"^sw_[A-Za-z0-9_]+$")
FRAME_NAME_RE = re.compile(r"^f_\d+\.jpg$")


def new_sweep(country: str | None, currency: str, root: Path | None = None, **extra) -> Path:
    sid = time.strftime("sw_%Y%m%d_%H%M%S_") + secrets.token_hex(2)
    sd = Path(root or CFG.sweeps_dir) / sid
    (sd / "frames").mkdir(parents=True)
    write(sd, "meta.json", {"sweep_id": sid, "country": country, "currency": currency,
                            "created_at": time.time(), **extra})
    write(sd, "frames.json", [])
    write(sd, "user_facts.json", [])
    return sd


def sweep_path(sweep_id: str, root: Path | None = None) -> Path:
    if not SWEEP_ID_RE.match(sweep_id):
        raise ValueError("bad sweep id")
    sd = Path(root or CFG.sweeps_dir) / sweep_id
    if not sd.is_dir():
        raise FileNotFoundError(sweep_id)
    return sd


def write(sd: Path, name: str, obj: Any) -> None:
    """Atomic JSON write (models, lists of models and plain dicts all work)."""
    dest = Path(sd) / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp")
    tmp.write_text(json.dumps(to_jsonable_python(obj), indent=2))
    os.replace(tmp, dest)


def read(sd: Path, name: str, tp: Any = dict, default: Any = None) -> Any:
    """Read + validate against `tp` (e.g. list[Sighting]). Missing file -> default."""
    p = Path(sd) / name
    if not p.exists():
        return default
    return TypeAdapter(tp).validate_json(p.read_text())


def load_image(sd: Path, rel: str) -> np.ndarray:
    img = cv2.imread(str(Path(sd) / rel))
    if img is None:
        raise FileNotFoundError(rel)
    return img


def mark_incomplete(sd: Path, stage: str) -> None:
    cur = read(sd, "incomplete.json", list[str], default=[])
    if stage not in cur:
        write(sd, "incomplete.json", cur + [stage])


def clear_incomplete(sd: Path, stage: str) -> None:
    cur = read(sd, "incomplete.json", list[str], default=[])
    if stage in cur:
        write(sd, "incomplete.json", [s for s in cur if s != stage])
