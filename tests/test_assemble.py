import dataclasses

import pytest
from pydantic import ValidationError

from backend import pipeline
from backend.core import store
from backend.stages import assemble
from backend.config import CFG
from backend.ports import Detection, SpineRead
from backend.schemas import Packet, Price
from tests.fakes import FakeCatalogue, FakeFX, FakeSource, FakeVision, cand, listing, make_sweep


def full_run(tmp_path):
    sd, _ = make_sweep(tmp_path)
    dets = [Detection((100, 50, 160, 450), "book", "upright"), Detection((300, 50, 360, 450), "book", "upright"),
            Detection((420, 100, 560, 400), "item")]
    v = FakeVision(dets, reads=[SpineRead("Dune", "Frank Herbert", None, True), SpineRead(None, None, None, False)])
    cat = FakeCatalogue({"dune": [cand("Dune", "Frank Herbert", isbn="123")]})
    src = FakeSource(new={"EBAY_US": [listing("Dune Frank Herbert", 12.5)]}, used={"EBAY_US": [listing("Dune Frank Herbert", p, "used") for p in (4, 5, 6)]})
    one_at_a_time = dataclasses.replace(CFG, crop_workers=1)      # the fake reader hands out reads in order
    pk = pipeline.run(sd, cfg=one_at_a_time, deps_={"vision": v, "catalogue": cat, "source": src, "fx": FakeFX(), "reconstructor": None})
    return sd, pk


def test_end_to_end_with_fakes(tmp_path):
    sd, pk = full_run(tmp_path)
    t = pk.totals
    assert t.book_count == 2 and t.books_identified == 1 and t.books_unidentified == 1
    assert t.books_replacement_cost == 12.5 and t.books_used_value == 5.0 and t.item_count == 1
    assert t.excluded_from_totals == 2                      # unidentified book + unpriced lamp
    reasons = {(r.ref_id, r.reason) for r in pk.review_queue}
    assert ("room", "room_scale_missing") in reasons and any(r == "unidentified" for _, r in reasons)
    for f in ("claim_packet.json", "report.html", "metrics.json", "voice_summary.txt"):
        assert (sd / f).exists()
    assert Packet.model_validate_json((sd / "claim_packet.json").read_text(), context={"sweep_dir": sd})


def test_changing_a_line_changes_totals(tmp_path):
    sd, pk = full_run(tmp_path)
    books = [b.model_copy(update={"replacement": b.replacement and b.replacement.model_copy(update={"amount": 99.0})}) for b in pk.books]
    assert assemble.compute_totals(books, pk.items, None, "USD").books_replacement_cost == 99.0


def test_price_without_source_is_rejected():
    with pytest.raises(ValidationError):
        Price(kind="replacement", amount=5.0, currency="USD")


def test_missing_frame_ref_is_rejected(tmp_path):
    sd, pk = full_run(tmp_path)
    d = pk.model_dump()
    d["books"][0]["frame_ref"] = "frames/nope.jpg"
    with pytest.raises(ValidationError):
        Packet.model_validate(d, context={"sweep_dir": sd})


def test_price_stage_failure_degrades_to_blank(tmp_path):
    sd, _ = make_sweep(tmp_path)
    class Boom(FakeSource):
        def search(self, *a, **k): raise RuntimeError("api down")
    dets = [Detection((100, 50, 160, 450), "book", "upright")]
    v = FakeVision(dets, reads=[SpineRead("Dune", "Frank Herbert", None, True)])
    pk = pipeline.run(sd, deps_={"vision": v, "catalogue": FakeCatalogue({"dune": [cand("Dune", "Frank Herbert")]}), "source": Boom(), "fx": FakeFX()})
    assert pk.totals.books_replacement_cost == 0 and pk.totals.excluded_from_totals == 1
