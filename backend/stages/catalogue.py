"""Catalogue-grounded identification (D5): a book's title, author, edition and ISBN are only ever taken from a
catalogue record that the spine itself confirms (resolve). The vision model's raw read is evidence, never the answer.

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


def author_surname(author: str | None) -> str:
    """'M. J. DeMarco', 'MJ DeMarco', 'DeMarco, M. J.' and 'Marc DeMarco' -> 'demarco': the same person however a
    catalogue spells the initials or orders the name. (Used to decide two records are the same work.)"""
    a = author or ""
    first = a.split(",")[0] if "," in a else a
    toks = norm(first).split()
    return toks[-1] if toks else ""


def work_key(c: Candidate) -> str:
    """Identity of a *work* (editions of one book collapse to one key): title + the author's surname."""
    return norm(c.title) + "|" + author_surname(c.author)


def _main_title(title: str) -> str:
    """'Sapiens: A Brief History of Humankind' -> 'Sapiens' (spines usually show only the main title)."""
    return re.split(r"\s*[:;(]\s*|\s+-\s+", title, maxsplit=1)[0]


def title_score(read_title: str | None, record_title: str | None) -> float:
    """Symmetric similarity, so 'Dune' does NOT match 'Dune Messiah' (a contained word is not a match).
    Compared with the full title and with the main title before a subtitle; the better one counts."""
    r = norm(read_title)
    if not r or not record_title:
        return 0.0
    return max(SequenceMatcher(None, r, norm(t)).ratio() for t in (record_title, _main_title(record_title)))


def author_score(read_author: str | None, record_author: str | None) -> float:
    """Order-insensitive and forgiving of a surname-only or truncated spine ('HERBERT', 'DALE CARNEG')."""
    return token_set_ratio(read_author, record_author) if read_author and record_author else 0.0


def same_author(a: str | None, b: str | None) -> bool:
    """Do two RECORDS name the same person? Symmetric and order-insensitive ('Herbert, Frank' == 'Frank Herbert'),
    but 'Jane Austen' != 'Jane Austen Leigh' (a contained name is not the same person)."""
    ta, tb = " ".join(sorted(norm(a).split())), " ".join(sorted(norm(b).split()))
    return bool(ta and tb) and SequenceMatcher(None, ta, tb).ratio() >= 0.9


@dataclass
class Match:
    candidate: Candidate | None
    score: float
    reason: str | None          # None when accepted
    candidates: list[Candidate]


def resolve(read: SpineRead, catalogue: Catalogue, cfg: Config = CFG) -> Match:
    """Strict identity. Accept a catalogue record only if BOTH the title and the author read from the spine
    match it, and no other matching record is a different book. Then, field by field:
      title, author  from the record (both verified against the spine)
      edition, year  only if the spine's publisher matches the record (and the record is one edition)
      isbn           only if the edition is verified AND the record is a single edition
    Anything not verified stays None. The model's own reading is never promoted to a field."""
    if not read.legible or not (read.title or "").strip():
        return Match(None, 0.0, "low_confidence", [])          # nothing to query: no call made
    if not (read.author or "").strip():
        return Match(None, 0.0, "author_not_visible", [])      # can't verify the author: no call, no guess
    try:
        cands = catalogue.search(read.title, read.author)
    except Exception as e:                                      # NFR-E5: degrade, don't crash
        log.warning("catalogue failed: %s", e)
        return Match(None, 0.0, "no_catalogue_match", [])
    if not cands:
        return Match(None, 0.0, "no_catalogue_match", [])
    scored = []
    for c in cands:
        t, a = title_score(read.title, c.title), author_score(read.author, c.author)
        scored.append((t >= cfg.title_match_T and a >= cfg.author_match_T, c.model_copy(update={"score": round(0.7 * t + 0.3 * a, 4)})))
    scored.sort(key=lambda x: (-x[0], -x[1].score))
    ranked = [c for _, c in scored]
    passing = [c for ok, c in scored if ok]
    if not passing:
        best = ranked[0]
        return Match(None, best.score, "no_catalogue_match" if best.score < 0.5 else "low_confidence", ranked)
    best = passing[0]
    # Two matching records that are different books (other title or other author) => we can't tell which: undefined.
    if any(title_score(best.title, c.title) < cfg.title_match_T or not same_author(best.author, c.author)
           for c in passing[1:]):
        return Match(None, best.score, "ambiguous_match", ranked)
    edition, year, isbn = None, None, None
    if read.publisher:
        pub = next((p for p in best.publishers if token_set_ratio(read.publisher, p) >= cfg.publisher_match_T), None)
        if pub:
            year = best.year if best.edition_level else None
            edition = pub + (f" {year}" if year else "")
            isbn = best.isbn if best.edition_level else None
    return Match(best.model_copy(update={"edition": edition, "year": year, "isbn": isbn}), best.score, None, ranked)


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
