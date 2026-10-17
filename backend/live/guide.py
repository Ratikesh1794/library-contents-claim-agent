"""GUIDE: what the voice agent proactively tells the user while they sweep. Code decides WHAT and WHEN; the agent only
speaks the `ask` word for word (design D10). Every message passes through one gate so the agent never talks over itself
or nags: a minimum gap between any two spoken messages, a cooldown per topic, and silence for 2 s after the user spoke.

Messages are {"type": "GUIDE", "key", "ask"}; their wording is in backend/prompts/coach.py (GUIDE_ASKS, BOOK_ASKS, GIVE_UP).
No clock of its own (callers pass `now`), so it is trivially testable.

Topics so far:
  slow   the phone could not get sharp frames (it moves too fast)                          on_blurry()
  book:* books in view that stay unreadable: hold steady / closer / glare / dark / blocked / pan   on_frame()
  giveup after `guide_book_asks` asks about a book: say we move on, then leave it alone    on_frame()
"""
from __future__ import annotations

from string import Template

from backend.config import CFG, Config
from backend.prompts.coach import BOOK_ASKS, GIVE_UP, GUIDE_ASKS

NEVER = float("-inf")

# Why a read failed -> what to ask. low_confidence = the text was there but not legible. author_not_visible,
# no_catalogue_match and ambiguous_match are NOT here: nothing the user can do about them.
HOW = {"blur": "hold", "low_confidence": "hold", "too_far": "closer", "glare": "glare", "too_dark": "dark",
       "blocked": "blocked", "partial": "partial"}
COLS, ROWS = ("left", "middle", "right"), ("top", "middle", "bottom")


def screen_event(m: dict) -> dict:
    """The WebSocket event the phone and dashboard show for a Coach/Guide message. Its type must be "coach": spreading
    the message over {"type": "coach"} overwrote it with CAPTURE_ISSUE/STATUS/GUIDE, which no screen recognises, so no
    tip was ever shown. The original type is kept as `kind`."""
    return {**m, "type": "coach", "kind": m["type"]}


def cell(box, w: int, h: int) -> tuple[str, str]:
    """Where a box sits in the frame: (left|middle|right, top|middle|bottom)."""
    cx, cy = (box[0] + box[2]) / 2 / max(w, 1), (box[1] + box[3]) / 2 / max(h, 1)
    return COLS[min(2, int(cx * 3))], ROWS[min(2, int(cy * 3))]


def where(cells: list[tuple[str, str]]) -> str:
    """'the books on the left' / 'the book at the top right' ... in words a person holding a phone can follow."""
    noun = "the book" if len(cells) == 1 else "the books"
    h = {"left": "on the left", "middle": "in the middle", "right": "on the right"}
    v = {"top": "at the top", "middle": "in the middle", "bottom": "at the bottom"}
    col = max(COLS, key=lambda c: sum(1 for x, _ in cells if x == c))
    row = max(ROWS, key=lambda r: sum(1 for _, y in cells if y == r))
    one_col = sum(1 for x, _ in cells if x == col) / len(cells) >= 0.6
    one_row = sum(1 for _, y in cells if y == row) / len(cells) >= 0.6
    if one_col and one_row:
        if col == row == "middle":
            return f"{noun} in the middle"
        if row == "middle":
            return f"{noun} {h[col]}"
        if col == "middle":
            return f"{noun} {v[row]}"
        return f"{noun} {v[row]} {col}"
    return f"{noun} {h[col]}" if one_col else (f"{noun} {v[row]}" if one_row else f"{noun} in view")


class Guide:
    def __init__(self, cfg: Config = CFG):
        self.cfg = cfg
        self.last_t = NEVER                                # when anything was last spoken (also by the Coach)
        self.last_by: dict[str, float] = {}                # topic -> when it was last said
        self.count: dict[str, int] = {}                    # topic -> times in a row (escalates the wording)

    def note_spoken(self, now: float) -> None:
        """Something else just spoke: keep the next guide message from landing on top of it."""
        self.last_t = max(self.last_t, now)

    def _gate(self, now: float, key: str, gap: float, last_user_speech_t: float) -> int | None:
        """May `key` be said now? -> how many times in a row it was said before (0 = first), or None for 'not now'."""
        if (now - self.last_t < self.cfg.guide_gap_s or now - self.last_by.get(key, NEVER) < gap
                or now - last_user_speech_t < 2.0):
            return None
        n = self.count.get(key, 0) if now - self.last_by.get(key, NEVER) < self.cfg.guide_repeat_window_s else 0
        self.count[key] = n + 1
        self.last_t = self.last_by[key] = now
        return n

    # ---------------------------------------------------------------- the phone cannot get sharp frames
    def on_blurry(self, now: float, last_user_speech_t: float = NEVER) -> dict | None:
        n = self._gate(now, "slow", self.cfg.guide_slow_gap_s, last_user_speech_t)
        if n is None:
            return None
        texts = GUIDE_ASKS["slow"]
        return {"type": "GUIDE", "key": "slow", "ask": texts[min(n, len(texts) - 1)]}

    # ---------------------------------------------------------------- books in view that stay unreadable
    def on_frame(self, now: float, w: int, h: int, view: list, last_user_speech_t: float = NEVER) -> dict | None:
        """`view` = [(sighting, book)] for the latest frame. Speak about the books that stay unreadable while they are
        in view; after `guide_book_asks` asks about a book, say once that we move on and stop asking about it."""
        c = self.cfg
        pending = []
        for s, b in view:
            m = b.meta
            fixable = (s.kind == "book" and not s.accepted and s.reason in HOW and not m.get("skip")
                       and not m.get("not_book") and m.get("read") is None)
            if not fixable:
                m.pop("pending_since", None)                       # read, skipped or not fixable: nothing to say
                continue
            m.setdefault("pending_since", now)
            if now - m["pending_since"] < c.guide_settle_s:        # give it a moment: the next frame may be sharper
                continue
            if now - m.get("asked_t", NEVER) < c.guide_book_gap_s:
                continue
            pending.append((s, b))
        if not pending:
            return None
        spent = [(s, b) for s, b in pending if b.meta.get("asked", 0) >= c.guide_book_asks]
        if spent:                                                  # asked enough: say so once, then leave them alone
            if self._gate(now, "giveup", 0.0, last_user_speech_t) is None:
                return None
            for _, b in spent:
                b.meta["skip"] = True
            return {"type": "GUIDE", "key": "giveup",
                    "ask": Template(GIVE_UP).substitute(what=where([cell(s.bbox, w, h) for s, _ in spent]))}
        how = max({HOW[s.reason] for s, _ in pending}, key=lambda k: sum(1 for s, _ in pending if HOW[s.reason] == k))
        group = [(s, b) for s, b in pending if HOW[s.reason] == how]
        if self._gate(now, f"book:{how}", 0.0, last_user_speech_t) is None:
            return None
        for _, b in group:
            b.meta["asked"], b.meta["asked_t"] = b.meta.get("asked", 0) + 1, now
        return {"type": "GUIDE", "key": f"book:{how}",
                "ask": Template(BOOK_ASKS[how]).substitute(what=where([cell(s.bbox, w, h) for s, _ in group]))}

    def skip(self, tracker, frame_id: str, region: str) -> int:
        """The user said 'skip this shelf': stop asking about the unreadable books in that row of the current view."""
        n = 0
        for s, b in tracker.view(frame_id):
            if s.shelf_hint == region and not s.accepted:
                b.meta["skip"] = True
                n += 1
        return n
