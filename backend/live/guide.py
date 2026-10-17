"""GUIDE: what the voice agent proactively tells the user while they sweep. Code decides WHAT and WHEN; the agent only
speaks the `ask` word for word (design D10, same as the Coach). Every message passes through one gate so the agent
never talks over itself or nags: a minimum gap between any two spoken messages, a per-topic cooldown, and silence for
2 s after the user last spoke.

Messages are {"type": "GUIDE", "key", "ask"}. The text lives in backend/prompts/coach.py (GUIDE_ASKS).
No clock of its own (callers pass `now`), so it is trivially testable.
"""
from __future__ import annotations

from backend.config import CFG, Config
from backend.prompts.coach import GUIDE_ASKS


def screen_event(m: dict) -> dict:
    """The WebSocket event the phone and dashboard show for a Coach/Guide message. Its type must be "coach": spreading
    the message over {"type": "coach"} overwrote it with CAPTURE_ISSUE/STATUS/GUIDE, which no screen recognises, so no
    tip was ever shown. The original type is kept as `kind`."""
    return {**m, "type": "coach", "kind": m["type"]}


class Guide:
    def __init__(self, cfg: Config = CFG):
        self.cfg = cfg
        self.last_t = float("-inf")                        # when anything was last spoken (also by the Coach)
        self.last_by: dict[str, float] = {}                # topic -> when it was last said
        self.count: dict[str, int] = {}                    # topic -> how many times in a row (escalates the wording)

    def note_spoken(self, now: float) -> None:
        """Something else (the Coach) just spoke: keep the next guide message from landing on top of it."""
        self.last_t = max(self.last_t, now)

    def _say(self, now: float, key: str, gap: float, last_user_speech_t: float) -> dict | None:
        if (now - self.last_t < self.cfg.guide_gap_s or now - self.last_by.get(key, float("-inf")) < gap
                or now - last_user_speech_t < 2.0):
            return None
        n = self.count.get(key, 0) if now - self.last_by.get(key, float("-inf")) < self.cfg.guide_repeat_window_s else 0
        self.count[key] = n + 1
        texts = GUIDE_ASKS[key]
        self.last_t = self.last_by[key] = now
        return {"type": "GUIDE", "key": key, "ask": texts[min(n, len(texts) - 1)]}

    def on_blurry(self, now: float, last_user_speech_t: float = float("-inf")) -> dict | None:
        """The phone rejected several frames in a row as too blurry: the camera is moving too fast."""
        return self._say(now, "slow", self.cfg.guide_slow_gap_s, last_user_speech_t)
