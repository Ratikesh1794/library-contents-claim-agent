"""Interpret UserFacts (voice corrections) against objects. Shared by price + assemble."""
from __future__ import annotations

from backend.schemas import Object, UserFact


def facts_for(obj: Object, facts: list[UserFact]) -> list[UserFact]:
    """A fact applies if its ref is the object id, its shelf/row, or appears in its title."""
    row = obj.shelf.split(":")[-1] if obj.shelf else None
    out = []
    for f in facts:
        ref = (f.ref or "").strip().lower()
        if not ref:
            continue
        if ref in (obj.id.lower(), (obj.shelf or "").lower(), (row or "").lower()):
            out.append(f)
        elif obj.title and ref in obj.title.lower():
            out.append(f)
    return out


def flags(obj: Object, facts: list[UserFact]) -> set[str]:
    """-> subset of {"signed","print","first_edition","skip"} from fact text."""
    out: set[str] = set()
    for f in facts_for(obj, facts):
        t = f.text.lower()
        if "sign" in t or "inscri" in t:
            out.add("signed")
        if "print" in t:
            out.add("print")
        if "first edition" in t:
            out.add("first_edition")
        if "skip" in t:
            out.add("skip")
    return out
