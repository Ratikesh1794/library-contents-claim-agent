"""Catalogue-grounded identification (D5): a title is only ever taken from a catalogue Candidate
that scored >= accept_T. The vision model's raw read is evidence, never the answer.

Pure logic lives here; HTTP clients live in backend/adapters/{open_library,google_books}.py.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from backend.config import CFG, Config
from backend.ports import Catalogue, SpineRead
from backend.schemas import Candidate

log = logging.getLogger(__name__)


def norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).strip()


def token_set_ratio(a: str | None, b: str | None) -> float:
    """0..1. Order-insensitive; extra words on one side are mostly forgiven (fuzzywuzzy-style)."""
    ta, tb = set(norm(a).split()), set(norm(b).split())
    if not ta or not tb:
        return 0.0
    inter = " ".join(sorted(ta & tb))
    s1 = (inter + " " + " ".join(sorted(ta - tb))).strip()
    s2 = (inter + " " + " ".join(sorted(tb - ta))).strip()
    r = SequenceMatcher
    return max(r(None, inter, s1).ratio() if inter else 0.0,
               r(None, inter, s2).ratio() if inter else 0.0,
               r(None, s1, s2).ratio())


def work_key(c: Candidate) -> str:
    """Identity of a *work* (editions of one book collapse to one key)."""
    return norm(c.title) + "|" + norm((c.author or "").split(",")[0])


def _score(read: SpineRead, c: Candidate) -> float:
    t = token_set_ratio(read.title, c.title)
    if read.author:                       # title-only spines are common; don't punish them
        return 0.7 * t + 0.3 * token_set_ratio(read.author, c.author)
    return t


@dataclass
class Match:
    candidate: Candidate | None
    score: float
    reason: str | None          # None when accepted
    candidates: list[Candidate]


def resolve(read: SpineRead, catalogue: Catalogue, cfg: Config = CFG) -> Match:
    if not read.legible or not (read.title or "").strip():
        return Match(None, 0.0, "low_confidence", [])          # nothing to query: no call made
    try:
        cands = catalogue.search(read.title, read.author)
    except Exception as e:                                      # NFR-E5: degrade, don't crash
        log.warning("catalogue failed: %s", e)
        return Match(None, 0.0, "no_catalogue_match", [])
    if not cands:
        return Match(None, 0.0, "no_catalogue_match", [])
    scored = sorted((c.model_copy(update={"score": _score(read, c)}) for c in cands),
                    key=lambda c: -c.score)
    best = scored[0]
    # Ambiguity: a *different* work scoring almost as well means we can't be sure which one it is.
    rivals = [c for c in scored if work_key(c) != work_key(best)
              and best.score - c.score <= cfg.ambiguity_gap]
    final = best.score - (cfg.ambiguity_penalty if rivals else 0.0)
    if final < cfg.accept_T:
        return Match(None, final, "no_catalogue_match" if best.score < 0.5 else "low_confidence", scored)
    # Edition only when the record exposes it AND the spine shows a matching publisher.
    edition = None
    if read.publisher:
        for p in best.publishers:
            if token_set_ratio(read.publisher, p) >= 0.85:
                edition = p + (f" {best.year}" if best.year else "")
                break
    return Match(best.model_copy(update={"score": final, "edition": edition}), final, None, scored)


class ChainCatalogue:
    """Try sources in order; the first one that returns candidates wins. Failures are skipped."""

    def __init__(self, sources: list[Catalogue]):
        self.sources = sources

    def search(self, title: str, author: str | None) -> list[Candidate]:
        for s in self.sources:
            try:
                out = s.search(title, author)
            except Exception as e:
                log.warning("%s failed: %s", type(s).__name__, e)
                continue
            if out:
                return out
        return []
