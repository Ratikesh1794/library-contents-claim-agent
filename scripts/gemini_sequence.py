"""Give Gemini an ordered series of frames from one sweep and let it decide which books are the same physical book.

    python scripts/gemini_sequence.py frame1.jpg frame2.jpg ...            # frames in the order they were taken
    python scripts/gemini_sequence.py shelf.jpg --slice 4                  # test: cut one photo into 4 overlapping frames

Output: one entry per DISTINCT physical book (title/author only if printed and legible, else null), the frames it
appears in, a short look description, and the total count. Same setup as gemini_books.py (GEMINI_API_KEY in .env).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))
import gemini_books as gb                                   # noqa: E402

PROMPT = """These {n} photos were taken IN ORDER while a camera moved along books on a shelf or in a pile. Consecutive photos
overlap, so the same physical book usually appears in several of them, from a slightly different position, distance
or lighting. Photos can also contain books not seen before (the camera moved left or right) and books cut off by the photo edge.
List every DISTINCT physical book exactly once, ordered left to right (top to bottom for a pile). For each book:
- frames: the photo numbers (1..{n}) it appears in, including photos where it is only partly visible
- title, author: copy ONLY text printed on that book and legible in at least one photo; otherwise null. Never use
  outside knowledge, never complete or correct text from memory.
- look: colour, size and anything distinctive, so the book can be recognised again
Two different books with the same title (two copies side by side) are two books. A book that appears in several photos
is ONE book. Also return the total count of distinct books."""


class Entry(BaseModel):
    frames: list[int]
    title: str | None
    author: str | None
    look: str


class Result(BaseModel):
    total_books: int
    books: list[Entry]


def slices(img: np.ndarray, n: int, size: float = 0.5) -> list[np.ndarray]:
    stacked = img.shape[0] > img.shape[1] * 1.15                  # tall photo: sweep up/down, else left/right
    axis = 0 if stacked else 1
    L = img.shape[axis]
    w = int(L * size)
    out = []
    for k in range(n):
        s = int(k * (L - w) / (n - 1))
        out.append(img[s:s + w] if axis == 0 else img[:, s:s + w])
    return out


def ask(frames: list[np.ndarray]) -> tuple[Result, int | None]:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    parts = []
    for k, f in enumerate(frames, 1):
        parts += [f"Photo {k}:", types.Part.from_bytes(data=cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tobytes(),
                                                       mime_type="image/jpeg")]
    for attempt in range(4):
        try:
            r = client.models.generate_content(
                model=gb.MODEL, contents=parts + [PROMPT.format(n=len(frames))],
                config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=Result, temperature=0))
            return r.parsed, getattr(r.usage_metadata, "total_token_count", None)
        except Exception as e:
            if attempt == 3 or getattr(e, "code", None) not in (429, 500, 503):
                raise
            time.sleep(10 * (attempt + 1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("images", nargs="+", type=Path)
    ap.add_argument("--slice", type=int, help="cut the single image into this many overlapping frames")
    ap.add_argument("--order", default="", help="comma list of frame numbers to send, e.g. 1,2,3,2,1 (repeats allowed)")
    a = ap.parse_args()
    frames = [gb.load(p)[0] for p in a.images]
    if a.slice:
        frames = slices(frames[0], a.slice)
    if a.order:
        frames = [frames[int(k) - 1] for k in a.order.split(",")]
    res, tokens = ask(frames)
    print(f"{len(frames)} frames -> Gemini says {res.total_books} distinct books (listed {len(res.books)}), tokens {tokens}")
    for i, b in enumerate(res.books, 1):
        print(f"  {i:>2}. frames {b.frames} | {b.title or '(unread)'} | {b.author or '-'} | {b.look[:60]}")
