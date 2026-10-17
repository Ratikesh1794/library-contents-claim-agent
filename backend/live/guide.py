"""GUIDE: what the voice agent proactively tells the user while they sweep. Code decides WHAT and WHEN; the agent only
speaks the `ask` word for word (design D10). Every message passes through one gate so the agent never talks over itself
or nags: a minimum gap between any two spoken messages, a cooldown per topic, and silence for 2 s after the user spoke.

Messages are {"type": "GUIDE", "key", "ask"}; their wording is in backend/prompts/coach.py (GUIDE_ASKS, BOOK_ASKS, GIVE_UP).
No clock of its own (callers pass `now`), so it is trivially testable.

Topics so far:
  slow   the phone could not get sharp frames (it moves too fast)                          on_blurry()
  book:* books in view that stay unreadable: hold steady / closer / glare / dark / blocked / pan   on_frame()
  giveup after `guide_book_asks` asks about a book: say we move on, then leave it alone    on_frame()
  back   unreadable books that left the view: go back to them (which way, from the shelf map)   on_frame(tracker=...)
  progress / idle   how many books so far; "this area looks done, move on or say finish"        on_tick()
  setup  before the sweep: camera focus, then the room size (AR, where the phone has it), then "walk the room"
         setup_event() / setup_tick(). Book guidance stays quiet until setup is done.
"""
from __future__ import annotations

from string import Template

from backend.config import CFG, Config
from backend.prompts.coach import BACK_ASK, BACK_WHERE, BOOK_ASKS, GIVE_UP, GUIDE_ASKS, IDLE, PROGRESS, SETUP

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
        self.best_count = 0                                # most books found so far
        self.milestone = 0                                 # progress already announced up to this many multiples
        self.progress_t = NEVER                            # when the book count last grew
        self.busy_t = NEVER                                # when a book in view last needed attention
        # setup: None until the phone says hello (older phones / tests skip it), then focus -> room -> start -> done
        self.setup: str | None = None
        self.setup_t = NEVER                               # when the current setup step began
        self.setup_asks = 0                                # asks in the current step
        self.ar = False                                    # the phone can measure the room (WebXR)
        self.room_area: float | None = None

    @property
    def in_setup(self) -> bool:
        return self.setup not in (None, "done")

    # ---------------------------------------------------------------- setup: focus, room size, then the sweep
    def _step(self, step: str, now: float) -> None:
        self.setup, self.setup_t, self.setup_asks = step, now, 0

    def setup_event(self, now: float, event: str, ar: bool = False, area: float | None = None) -> None:
        """hello (phone connected; `ar` = it can measure the room), focused (a steady sharp picture), room (measured)."""
        if event == "hello" and self.setup is None:
            self.ar = ar
            self._step("focus", now)
        elif event == "focused" and self.setup == "focus":
            self._step("room" if self.ar else "start", now)
        elif event == "room" and self.in_setup:
            self.room_area = area
            self._step("start", now)

    def setup_tick(self, now: float, last_user_speech_t: float = NEVER) -> dict | None:
        """What to say for the current setup step, if anything. A step asked `setup_asks` times without an answer is
        given up (the sweep matters more than a perfect setup)."""
        c = self.cfg
        if not self.in_setup:
            return None
        if self.setup == "start":
            if self._gate(now, "setup:start", 0.0, last_user_speech_t) is None:
                return None
            if self.room_area is not None:
                ask = Template(SETUP["start_measured"]).substitute(area=f"{self.room_area:g}")
            else:
                ask = SETUP["start_skipped"] if self.ar else SETUP["start_no_ar"]
            self._step("done", now)
            self.progress_t = now                          # the sweep starts now: "area looks done" counts from here
            return {"type": "GUIDE", "key": "setup:start", "ask": ask}
        if self.setup_asks >= c.setup_asks and now - self.setup_t >= c.setup_repeat_s:     # no answer: move on
            self._step("room" if self.setup == "focus" and self.ar else "start", now)
            return self.setup_tick(now, last_user_speech_t)
        wait = c.setup_first_s if self.setup_asks == 0 else c.setup_repeat_s
        if now - self.setup_t < wait or self._gate(now, f"setup:{self.setup}", 0.0, last_user_speech_t) is None:
            return None
        texts = SETUP[self.setup]
        self.setup_asks += 1
        self.setup_t = now
        return {"type": "GUIDE", "key": f"setup:{self.setup}", "ask": texts[min(self.setup_asks - 1, len(texts) - 1)]}

    def note_spoken(self, now: float) -> None:
        """Something else just spoke: keep the next guide message from landing on top of it."""
        self.last_t = max(self.last_t, now)

    def _gate(self, now: float, key: str, gap: float, last_user_speech_t: float, window: float | None = None) -> int | None:
        """May `key` be said now? -> how many times in a row it was said before (0 = first), or None for 'not now'.
        Saying it again within `window` counts as 'in a row' (firmer wording)."""
        if (now - self.last_t < self.cfg.guide_gap_s or now - self.last_by.get(key, NEVER) < gap
                or now - last_user_speech_t < 2.0):
            return None
        window = self.cfg.guide_repeat_window_s if window is None else window
        n = self.count.get(key, 0) if now - self.last_by.get(key, NEVER) < window else 0
        self.count[key] = n + 1
        self.last_t = self.last_by[key] = now
        return n

    # ---------------------------------------------------------------- the phone cannot get sharp frames
    def on_blurry(self, now: float, last_user_speech_t: float = NEVER) -> dict | None:
        n = None if self.in_setup else self._gate(now, "slow", self.cfg.guide_slow_gap_s, last_user_speech_t)
        if n is None:
            return None
        texts = GUIDE_ASKS["slow"]
        return {"type": "GUIDE", "key": "slow", "ask": texts[min(n, len(texts) - 1)]}

    # ---------------------------------------------------------------- books in view that stay unreadable
    def on_frame(self, now: float, w: int, h: int, view: list, last_user_speech_t: float = NEVER,
                 tracker=None) -> dict | None:
        """`view` = [(sighting, book)] for the latest frame. Speak about the books that stay unreadable while they are
        in view; after `guide_book_asks` asks about a book, say once that we move on and stop asking about it. With a
        `tracker`, books passed unread (now out of view) are asked about last, when nothing in view needs attention."""
        if self.in_setup:
            return None
        msg, about_to_ask = self._in_view(now, w, h, view, last_user_speech_t)
        if msg is None and not about_to_ask and tracker is not None:
            msg = self._come_back(now, tracker, view, last_user_speech_t)
        return msg

    @staticmethod
    def _unresolved(m: dict) -> bool:
        return m.get("read") is None and not m.get("not_book") and m.get("reason") in HOW

    def _in_view(self, now: float, w: int, h: int, view: list, last_user_speech_t: float) -> tuple[dict | None, bool]:
        """-> (message, about_to_ask): about_to_ask is True while a book in view is still in its settling window, so
        something about it is likely to be said in a moment and 'go back' should wait."""
        c = self.cfg
        pending, about_to_ask = [], False
        for s, b in view:
            m = b.meta
            m["seen_t"] = now                                      # last time it was in front of the camera
            fixable = (s.kind == "book" and not s.accepted and s.reason in HOW and not m.get("skip")
                       and not m.get("not_book") and m.get("read") is None)
            if not fixable:
                m.pop("pending_since", None)                       # read, skipped or not fixable: nothing to say
                continue
            self.busy_t = now                                      # the user is working on a book: not "done" yet
            m.setdefault("pending_since", now)
            if now - m["pending_since"] < c.guide_settle_s:        # give it a moment: the next frame may be sharper
                about_to_ask = True
                continue
            if now - m.get("asked_t", NEVER) < c.guide_book_gap_s:
                continue
            pending.append((s, b))
        if not pending:
            return None, about_to_ask
        spent = [(s, b) for s, b in pending if b.meta.get("asked", 0) >= c.guide_book_asks]
        if spent:                                                  # asked enough: say so once, then leave them alone
            if self._gate(now, "giveup", 0.0, last_user_speech_t) is None:
                return None, about_to_ask
            for _, b in spent:
                b.meta["skip"] = True
            return ({"type": "GUIDE", "key": "giveup",
                     "ask": Template(GIVE_UP).substitute(what=where([cell(s.bbox, w, h) for s, _ in spent]))}, about_to_ask)
        how = max({HOW[s.reason] for s, _ in pending}, key=lambda k: sum(1 for s, _ in pending if HOW[s.reason] == k))
        group = [(s, b) for s, b in pending if HOW[s.reason] == how]
        if self._gate(now, f"book:{how}", 0.0, last_user_speech_t) is None:
            return None, about_to_ask
        for _, b in group:
            b.meta["asked"], b.meta["asked_t"] = b.meta.get("asked", 0) + 1, now
        return ({"type": "GUIDE", "key": f"book:{how}",
                 "ask": Template(BOOK_ASKS[how]).substitute(what=where([cell(s.bbox, w, h) for s, _ in group]))}, about_to_ask)

    def _come_back(self, now: float, tracker, view: list, last_user_speech_t: float) -> dict | None:
        """Unreadable books that left the view: say which way they are. Direction comes from the shelf map, in what the
        user sees (left/right/higher/lower), and only the nearest side is mentioned."""
        c = self.cfg
        near = []
        for b, x, y, fw, fh in tracker.offscreen({id(b) for _, b in view}):
            m = b.meta
            if (not self._unresolved(m) or m.get("skip") or "seen_t" not in m or now - m["seen_t"] < c.guide_moved_on_s
                    or m.get("recalled", 0) >= c.guide_back_asks
                    or now - m.get("recalled_t", NEVER) < c.guide_back_gap_s):
                continue
            dx, dy = max(-x, x - fw, 0.0) / fw, max(-y, y - fh, 0.0) / fh
            if max(dx, dy) < 0.1:                                  # still (nearly) in view
                continue
            side = ("left" if x < 0 else "right") if dx >= dy else ("above" if y < 0 else "below")
            near.append((max(dx, dy), side, b))
        if not near:
            return None
        side = min(near, key=lambda t: t[0])[1]
        group = [b for _, sd, b in near if sd == side]
        if self._gate(now, "back", c.guide_back_gap_s, last_user_speech_t) is None:
            return None
        for b in group:
            b.meta["recalled"], b.meta["recalled_t"] = b.meta.get("recalled", 0) + 1, now
        n = len(group)
        what = "the book" if n == 1 else ({2: "two books", 3: "three books"}.get(n, "a few books"))
        return {"type": "GUIDE", "key": "back", "ask": Template(BACK_ASK).substitute(
            what=what, where=BACK_WHERE[side], it="it" if n == 1 else "them")}

    # ---------------------------------------------------------------- progress and "this area looks done"
    def on_tick(self, now: float, count: int, last_user_speech_t: float = NEVER) -> dict | None:
        """Called every couple of seconds (not only when a frame arrives: a user holding still sends none).
        `count` = distinct books found so far. Says how many were found every `guide_progress_every` books; after
        `guide_idle_s` with no new book and nothing needing attention, says the area looks done."""
        c = self.cfg
        if self.in_setup:
            return self.setup_tick(now, last_user_speech_t)
        if count > self.best_count:
            self.best_count, self.progress_t = count, now
            if count // c.guide_progress_every > self.milestone:
                self.milestone = count // c.guide_progress_every
                if self._gate(now, "progress", c.guide_progress_gap_s, last_user_speech_t) is not None:
                    return {"type": "GUIDE", "key": "progress", "ask": Template(PROGRESS).substitute(n=count)}
            return None
        if self.best_count == 0:
            return None
        quiet_since = max(self.progress_t, self.busy_t, self.last_t)   # nothing new, nothing wrong, nothing just said
        if now - quiet_since < c.guide_idle_s:
            return None
        n = self._gate(now, "idle", c.guide_idle_gap_s, last_user_speech_t, window=3 * c.guide_idle_gap_s)
        if n is None:
            return None
        return {"type": "GUIDE", "key": "idle", "ask": IDLE[min(n, len(IDLE) - 1)]}

    def skip(self, tracker, frame_id: str, region: str) -> int:
        """The user said 'skip this shelf': stop asking about the unreadable books in that row of the current view."""
        n = 0
        for s, b in tracker.view(frame_id):
            if s.shelf_hint == region and not s.accepted:
                b.meta["skip"] = True
                n += 1
        return n
