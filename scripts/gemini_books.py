"""Book count + metadata from one photo with a Gemini model (free tier). One call per image, nothing else.

    python scripts/gemini_books.py shelf.jpg [more.jpg ...]     # -> shelf_gemini.jpg (annotated) + shelf_gemini.json

Setup: get a free key at https://aistudio.google.com/apikey and put  GEMINI_API_KEY=...  in .env.
Model: GEMINI_MODEL in .env (default gemini-3.8-flash). Free-tier limits apply (requests per minute / per day).

The prompt forbids guessing: title, author and edition must be printed on the book and fully legible, else null.
No confidence cut-off: Gemini's own score is not a calibrated probability (it says 0.95 about nearly everything), so
it is only reported, never used to drop a read. The guard against guesses is the prompt (null unless printed and legible).
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Literal

import cv2
import numpy as np
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

PROMPT = """You are reading a photo of books. List EVERY book you can see, including partly visible ones, each exactly once.
For each book return:
- box_2d: [ymin, xmin, ymax, xmax] of that single book, normalised to 0-1000
- placement: "stacked" if the book lies flat (spine horizontal) or "vertical" if it stands upright (spine vertical)
- title, author, edition: copy ONLY text that is printed on this book and fully legible. If any of them is not printed,
  cut off, blurred, or you would have to guess or recall it from memory, return null. Never complete a title from
  memory, never use outside knowledge, never fix spelling. Edition means words like "2nd Edition" or "Revised Edition".
- confidence: 0 to 1, how certain you are that the text you returned is exactly what is printed
Also count the books and say whether the photo shows stacked books, vertical books, or both."""


class Book(BaseModel):
    box_2d: list[int] = Field(description="[ymin, xmin, ymax, xmax], 0-1000")
    placement: Literal["stacked", "vertical"]
    title: str | None
    author: str | None
    edition: str | None
    confidence: float


class Scan(BaseModel):
    layout: Literal["stacked", "vertical", "both"]
    book_count: int
    books: list[Book]


def load(path: Path) -> tuple[np.ndarray, bytes]:
    img = cv2.imread(str(path))
    if img is None:                                     # webp / avif
        from PIL import Image
        img = cv2.cvtColor(np.array(Image.open(path).convert("RGB")), cv2.COLOR_RGB2BGR)
    ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])
    return img, jpg.tobytes()


def read_image(img: np.ndarray) -> tuple[list[dict], dict]:
    """One Gemini call on a BGR image -> (books, info). Each book: layout, title, author, edition, confidence and
    box = [x0, y0, x1, y1] in pixels. info: count_reported, usage (tokens)."""
    from google import genai
    from google.genai import types
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise SystemExit("Set GEMINI_API_KEY in .env (free key: https://aistudio.google.com/apikey)")
    jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tobytes()
    client = genai.Client(api_key=key)                  # keep a reference: a temporary client is closed mid-request
    for attempt in range(4):                            # the free tier answers 503 "high demand" now and then
        try:
            resp = client.models.generate_content(
                model=MODEL, contents=[types.Part.from_bytes(data=jpg, mime_type="image/jpeg"), PROMPT],
                config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=Scan, temperature=0))
            break
        except Exception as e:
            if attempt == 3 or getattr(e, "code", None) not in (429, 500, 503):
                raise
            time.sleep(10 * (attempt + 1))
    result: Scan = resp.parsed
    H, W = img.shape[:2]
    books = [{"layout": b.placement, "title": b.title, "author": b.author, "edition": b.edition, "confidence": b.confidence,
              "box": [int(b.box_2d[1] * W / 1000), int(b.box_2d[0] * H / 1000),
                      int(b.box_2d[3] * W / 1000), int(b.box_2d[2] * H / 1000)]} for b in result.books]
    return books, {"count_reported": result.book_count, "usage": getattr(resp.usage_metadata, "total_token_count", None)}


def scan(path: Path) -> dict:
    img, _ = load(path)
    books, info = read_image(img)
    n_id = sum(1 for b in books if b["title"] and b["author"])
    layouts = {b["layout"] for b in books}
    report = {"image": str(path), "model": MODEL, "count_reported": info["count_reported"],
              "count_listed": len(books), "identified": n_id,
              "layout": "both" if len(layouts) == 2 else (layouts.pop() if layouts else "none"),
              "stacked": sum(b["layout"] == "stacked" for b in books), "vertical": sum(b["layout"] == "vertical" for b in books),
              "books": books, "usage": info["usage"]}
    annotate(img, books, report, path)
    path.with_name(path.stem + "_gemini.json").write_text(json.dumps(report, indent=2))
    return report


def annotate(img: np.ndarray, books: list[dict], rep: dict, path: Path) -> None:
    vis, s = img.copy(), max(img.shape[:2]) / 1000
    for i, b in enumerate(books, 1):
        x0, y0, x1, y1 = b["box"]
        color = (60, 180, 60) if b["title"] and b["author"] else (0, 140, 255)
        cv2.rectangle(vis, (x0, y0), (x1, y1), color, max(2, int(3 * s)))
        cv2.putText(vis, str(i), (x0 + 4, y0 + int(22 * s)), cv2.FONT_HERSHEY_SIMPLEX, 0.7 * s, (255, 255, 255), max(3, int(5 * s)))
        cv2.putText(vis, str(i), (x0 + 4, y0 + int(22 * s)), cv2.FONT_HERSHEY_SIMPLEX, 0.7 * s, (0, 0, 0), max(1, int(2 * s)))
    rows = [f"Books: {rep['count_listed']}   identified: {rep['identified']}   layout: {rep['layout']}",
            "green = title+author read, orange = not read (null, no guessing)", ""]
    for i, b in enumerate(books, 1):
        what = f"{b['title']} - {b['author']}" + (f" ({b['edition']})" if b["edition"] else "") if b["title"] else "unidentified"
        rows.append(f"{i}. {what}  [{b['layout']}, conf {b['confidence']:.2f}]")
    lh = 22
    panel = np.full((max(vis.shape[0], lh * (len(rows) + 2)), 700, 3), 255, np.uint8)
    for k, row in enumerate(rows):
        cv2.putText(panel, row[:88], (12, lh * (k + 1) + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (20, 20, 20), 1, cv2.LINE_AA)
    if vis.shape[0] < panel.shape[0]:
        vis = cv2.copyMakeBorder(vis, 0, panel.shape[0] - vis.shape[0], 0, 0, cv2.BORDER_CONSTANT, value=(255, 255, 255))
    cv2.imwrite(str(path.with_name(path.stem + "_gemini.jpg")), cv2.hconcat([vis, panel]))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("images", nargs="+", type=Path)
    a = ap.parse_args()
    for p in a.images:
        r = scan(p)
        print(f"\n{p.name}: {r['count_listed']} books ({r['layout']}: {r['stacked']} stacked, {r['vertical']} vertical), "
              f"{r['identified']} identified")
        for i, b in enumerate(r["books"], 1):
            if b["title"]:
                print(f"  {i:>3}. {b['title']} — {b['author']}" + (f" ({b['edition']})" if b["edition"] else ""))
