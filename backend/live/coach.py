"""COACH: capture problems are detected by CODE and only SPOKEN by the agent (design D10/D11).

Feed it each new frame's sightings; it returns messages for the agent/UI:
    {"type": "CAPTURE_ISSUE", "region", "reason", "ask"}   speak this one short sentence
    {"type": "STATUS", "text", "closed_region"}            region given up on (after asks_per_region asks)

Rules: one message at a time, >= ask_gap_s apart, wait settle_s after the first failing frame,
stay quiet if the user spoke in the last 2 s, never ask for stack_hidden, max 2 asks per region.
A "region" is the sighting's shelf_hint (row N from the top of the frame).

Not built yet: "missed shelf" coverage map (FR-L3 part), see README.
What the agent says for each reason is in backend/prompts/coach.py (ASKS).
This class has no clock of its own (pass `now`), so it is trivially testable.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from backend.config import CFG, Config
from backend.prompts import fill
from backend.prompts.coach import ASKS
from backend.schemas import Sighting

ASKABLE = ("too_far", "blur", "glare", "too_dark", "blocked", "partial")


@dataclass
class Region:
    id: str
    ask_count: int = 0
    last_ask_t: float = float("-inf")
    first_fail_t: float | None = None
    state: str = "open"                      # open | resolved | closed
    reasons: Counter = field(default_factory=Counter)   # failing reasons in the latest frame

    @property
    def label(self) -> str:
        return f"shelf {self.id.replace('row', 'row ')}" if self.id.startswith("row") else self.id


class Coach:
    def __init__(self, cfg: Config = CFG):
        self.cfg, self.regions, self.last_ask_t = cfg, {}, float("-inf")

    def close(self, region_id: str) -> None:
        """User said skip: stop asking about this region."""
        self.regions.setdefault(region_id, Region(region_id)).state = "closed"

    def update(self, sightings: list[Sighting], now: float, last_user_speech_t: float = float("-inf")) -> list[dict]:
        c, msgs = self.cfg, []
        by_region: dict[str, list[Sighting]] = {}
        for s in sightings:
            if s.kind == "book":
                by_region.setdefault(s.shelf_hint or "row?", []).append(s)
        for rid, ss in by_region.items():
            r = self.regions.setdefault(rid, Region(rid))
            if r.state == "closed":
                continue
            ok = sum(1 for s in ss if s.accepted)
            failing = Counter(s.reason for s in ss if not s.accepted and s.reason in ASKABLE)
            if r.first_fail_t is not None and ok / max(1, ok + sum(failing.values())) >= c.resolve_ratio:
                r.state, r.first_fail_t, r.reasons = "resolved", None, Counter()   # fixed by the latest frame
                continue
            r.state, r.reasons = "open", failing
            if failing and r.first_fail_t is None:
                r.first_fail_t = now
            if not failing:
                r.first_fail_t = None
        # give up on regions that were asked twice and are still failing
        for r in self.regions.values():
            if (r.state == "open" and r.ask_count >= c.asks_per_region and r.reasons
                    and now - r.last_ask_t >= c.ask_gap_s):
                r.state = "closed"
                msgs.append({"type": "STATUS", "closed_region": r.id,
                             "text": f"I'll mark {r.label} as unreadable and move on."})
        # at most one new ask
        if now - self.last_ask_t >= c.ask_gap_s and now - last_user_speech_t >= 2.0:
            cands = [r for r in self.regions.values()
                     if r.state == "open" and r.reasons and r.ask_count < c.asks_per_region
                     and r.first_fail_t is not None and now - r.first_fail_t >= c.settle_s
                     and now - r.last_ask_t >= c.ask_gap_s]
            if cands:
                r = max(cands, key=lambda r: sum(r.reasons.values()))
                reason = r.reasons.most_common(1)[0][0]
                r.ask_count, r.last_ask_t, self.last_ask_t = r.ask_count + 1, now, now
                msgs.append({"type": "CAPTURE_ISSUE", "region": r.id, "reason": reason,
                             "ask": fill(ASKS[reason], region=r.label)})
        return msgs
