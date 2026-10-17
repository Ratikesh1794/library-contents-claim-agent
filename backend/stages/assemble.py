"""ASSEMBLE stage: objects + geometry + room + prices + user_facts  ->  claim_packet.json (+ report, summary)

Everything numeric is computed here in code (FR-K3, NFR-I4): `compute_totals` is a pure function
used to build the totals AND to re-check them after validation.

Review queue: one entry per line, one reason, highest priority first (REVIEW_PRIORITY).
"""
from __future__ import annotations

import time
from collections.abc import Iterable
from pathlib import Path

from backend.core import facts, store
from backend.config import CFG, Config
from backend.schemas import (BookLine, BookDims, Geometry, ItemLine, Object, Packet, Price, PriceResult,
                             ReviewRef, Room, Totals, UserFact)

REVIEW_PRIORITY = ["needs_appraisal", "unidentified", "no_price", "low_confidence",
                   "converted", "wide_range", "no_scale", "scale_suspect"]


def _total(values: Iterable[float | None]) -> float:
    """Sum the known amounts; unknown (None) never counts as 0, it is simply not in the total."""
    return round(sum(v for v in values if v is not None), 2)


def compute_totals(books: list[BookLine], items: list[ItemLine], shelf_run_m: float | None, currency: str) -> Totals:
    """Totals computed in code from the lines (never by a model). Lines without a counted price are excluded and counted."""
    def counted_book(b: BookLine) -> bool:
        return b.status != "needs_appraisal" and any(p is not None and p.amount is not None for p in (b.replacement, b.used))

    def counted_item(i: ItemLine) -> bool:
        return i.status in ("priced", "range") and i.price is not None and i.price.low is not None

    priced_books = [b for b in books if b.status != "needs_appraisal"]
    item_prices: list[Price] = []
    for line in items:
        if counted_item(line) and line.price is not None:
            item_prices.append(line.price)
    return Totals(
        currency=currency, book_count=len(books),
        books_identified=sum(b.status == "identified" for b in books),
        books_unidentified=sum(b.status == "unidentified" for b in books),
        books_needs_appraisal=sum(b.status == "needs_appraisal" for b in books),
        item_count=len(items), shelf_run_m=shelf_run_m,
        books_replacement_cost=_total(b.replacement.amount if b.replacement is not None else None for b in priced_books),
        books_used_value=_total(b.used.amount if b.used is not None else None for b in priced_books),
        items_replacement_cost_low=_total(p.low for p in item_prices),
        items_replacement_cost_high=_total(p.high for p in item_prices),
        excluded_from_totals=sum(not counted_book(b) for b in books) + sum(not counted_item(i) for i in items),
    )


def _review_reason(reasons: set[str]) -> str | None:
    return next((r for r in REVIEW_PRIORITY if r in reasons), None)


def build(sd: Path, cfg: Config = CFG) -> Packet:
    """Combine objects, measurements, prices and user facts into the contract packet, with one review-queue
    reason per doubtful line (REVIEW_PRIORITY), room/stage problems, and validation against the saved frames."""
    meta = store.read(sd, "meta.json")
    objs = store.read(sd, "objects.json", list[Object], default=[])
    geo = store.read(sd, "geometry.json", Geometry, default=Geometry())
    room = store.read(sd, "room.json", Room, default=None)
    prices: dict[str, PriceResult] = {p.object_id: p for p in store.read(sd, "prices.json", list[PriceResult], default=[])}
    user_facts: list[UserFact] = store.read(sd, "user_facts.json", list[UserFact], default=[])
    incomplete = store.read(sd, "incomplete.json", list[str], default=[])
    cur = meta["currency"]
    books, items, review = [], [], []

    for o in objs:
        f = facts.flags(o, user_facts)
        notes = [x.text for x in facts.facts_for(o, user_facts)]
        pr: PriceResult | None = prices.get(o.id)
        reasons: set[str] = set()
        if o.kind == "book":
            status, reason = o.status, o.reason
            if "skip" in f:
                status, reason = "unidentified", "user_skipped"
            if pr is not None and pr.status == "needs_appraisal":
                status, reason = "needs_appraisal", pr.reason
            dims = geo.books.get(o.id, BookDims(reason="no_scale"))
            line = BookLine(
                id=o.id, status=status, title=o.title, author=o.author, edition=o.edition, isbn=o.isbn,
                catalogue_match_score=o.catalogue_match_score, id_confidence=o.id_confidence, reason=reason,
                spine_height_cm=dims.height_cm, spine_thickness_cm=dims.thickness_cm, dims_unc_cm=dims.unc_cm,
                frame_ref=o.frame_ref, bbox=o.bbox, user_notes=notes,
                replacement=pr.replacement if pr is not None and status != "needs_appraisal" else None,
                used=pr.used if pr is not None and status != "needs_appraisal" else None)
            books.append(line)
            if status == "needs_appraisal":
                reasons.add("needs_appraisal")
            if status == "unidentified":
                reasons.add("unidentified")
            elif status == "identified" and not (line.replacement or line.used):
                reasons.add("no_price")
            if o.id_confidence is not None and o.id_confidence < cfg.review_T and status == "identified":
                reasons.add("low_confidence")
            if any(p and p.converted for p in (line.replacement, line.used)):
                reasons.add("converted")
            if dims.reason == "no_scale":
                reasons.add("no_scale")
            if geo.scale_suspect:
                reasons.add("scale_suspect")
            detail = reason
        else:
            status = pr.status if pr is not None else "unpriced"
            line = ItemLine(
                id=o.id, status=status, category=o.category, description=o.name, material=o.material, brand_model=o.brand_model,
                dims_hint=o.dims_hint, id_confidence=o.id_confidence, frame_ref=o.frame_ref, bbox=o.bbox,
                price=pr.replacement if pr is not None and status != "needs_appraisal" else None,
                reason=pr.reason if pr is not None else "not_priced", user_notes=notes)
            items.append(line)
            if status == "needs_appraisal":
                reasons.add("needs_appraisal")
            elif status == "unpriced":
                reasons.add("no_price")
            elif line.price and line.price.converted:
                reasons.add("converted")
            if pr is not None and pr.reason == "wide_range":
                reasons.add("wide_range")
            if o.status == "unidentified":
                reasons.add("unidentified")
            detail = line.reason
        rr = _review_reason(reasons)
        if rr:
            review.append(ReviewRef(ref_id=o.id, reason=rr, detail=detail))

    if room is None or room.floor_area_m2 is None:
        review.append(ReviewRef(ref_id="room", reason="room_scale_missing", detail="; ".join(room.notes) if room else None))
    elif geo.shelved_wall_area_m2 is not None:
        room.shelved_wall_area_m2 = geo.shelved_wall_area_m2
        room.shelved_wall_area_ft2 = round(geo.shelved_wall_area_m2 * 10.7639, 1)
    for st in incomplete:
        review.append(ReviewRef(ref_id=st, reason="stage_incomplete", detail=f"{st} ran out of time budget"))

    limit = cfg.appraisal_threshold_usd
    return Packet(
        sweep_id=meta["sweep_id"], country=meta.get("country"), currency=cur,
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), room=room,
        totals=compute_totals(books, items, geo.shelf_run_m, cur), books=books, items=items, review_queue=review,
        assumptions=[
            f"Replacement cost = lowest credible NEW listing; used value = median of >= {cfg.used_min_listings} USED listings, condition assumed '{cfg.condition_assumed}'.",
            f"Books or items above {limit:.0f} USD (converted at runtime), signed, or older than {cfg.antiquarian_year} go to appraisal and are not priced.",
            "Wall area is gross (doors/windows not subtracted).",
            "Prices marked converted were fetched in another market and converted with the ECB rate stored on the line.",
            "Blank means unknown; unknowns are excluded from totals and listed in the review queue.",
        ])


def voice_summary(p: Packet) -> str:
    """Template, not an LLM: the agent reads this, it does no arithmetic."""
    t = p.totals
    parts = [f"I found {t.book_count} books, {t.books_identified} identified and {t.books_unidentified} I could not read."]
    if t.books_replacement_cost:
        parts.append(f"Replacement cost for the priced books is {t.books_replacement_cost:.2f} {t.currency}.")
    if t.item_count:
        parts.append(f"I also found {t.item_count} other items.")
    if p.room and p.room.floor_area_m2:
        parts.append(f"The floor area is about {p.room.floor_area_m2:.1f} square metres.")
    parts.append(f"{len(p.review_queue)} lines need your review.")
    return " ".join(parts)


def run(sd: Path, cfg: Config = CFG) -> Packet:
    sd = Path(sd)
    packet = build(sd, cfg)
    # Validation: schema + frame_refs exist + totals recompute identically.
    checked = Packet.model_validate(packet.model_dump(), context={"sweep_dir": sd})
    again = compute_totals(checked.books, checked.items, checked.totals.shelf_run_m, checked.currency)
    assert again == checked.totals, "totals do not match lines"
    store.write(sd, "claim_packet.json", checked)
    (sd / "voice_summary.txt").write_text(voice_summary(checked))
    from backend.stages import report
    report.render(sd, checked)
    return checked
