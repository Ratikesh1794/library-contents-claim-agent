"""REPORT: claim_packet.json -> report.html, a single self-contained file.

Every line's evidence crop is embedded as a small data: URI thumbnail, so the report needs no extra
routes or files: it works served by the app (/api/sweeps/<id>/report), opened from disk, or shared.
Template: backend/templates/report.html.j2 (edit the look there)."""
from __future__ import annotations

import base64
from pathlib import Path

import cv2
from jinja2 import Environment, FileSystemLoader, select_autoescape

from backend.schemas import Packet

THUMB_MAX_PX = 160   # long side of embedded thumbnails; keeps the report light (~5 KB per line)


def render(sd: Path, p: Packet) -> Path:
    sd = Path(sd)
    frames: dict[str, object] = {}
    thumbs = {}
    for line in [*p.books, *p.items]:
        if line.frame_ref not in frames:
            frames[line.frame_ref] = cv2.imread(str(sd / line.frame_ref))
        img = frames[line.frame_ref]
        if img is None:
            continue
        x0, y0, x1, y1 = line.bbox
        crop = img[max(0, y0):y1, max(0, x0):x1]
        if not crop.size:
            continue
        f = THUMB_MAX_PX / max(crop.shape[:2])
        if f < 1:
            crop = cv2.resize(crop, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            thumbs[line.id] = "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()
    env = Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"),
                      autoescape=select_autoescape(["html", "j2"]))
    out = sd / "report.html"
    out.write_text(env.get_template("report.html.j2").render(p=p, t=p.totals, thumbs=thumbs))
    return out
