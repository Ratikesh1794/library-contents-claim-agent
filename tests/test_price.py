import dataclasses

from backend.core import store
from backend.stages import price
from backend.config import CFG
from backend.schemas import ItemGuess, Object, PriceResult, UserFact
from tests.fakes import FakeFX, FakeSource, listing, make_sweep


def book(**kw):
    d = dict(id="b_001", kind="book", sighting_ids=["s"], status="identified", frame_ref="frames/f_0000.jpg", bbox=(0, 0, 1, 1),
             title="Dune", author="Frank Herbert", id_confidence=0.95, shelf="f_0000:row1")
    return Object(**{**d, **kw})


def go(o, src, facts=(), cur="USD", country="US", fx=None):
    fx = fx or FakeFX()
    return price.price_book(o, list(facts), cur, country, src, fx, CFG)


NEW = [listing("Dune Frank Herbert paperback", 12.5), listing("Dune Frank Herbert hardcover", 20.0), listing("Dune Messiah Herbert", 5.0)]
USED = [listing("Dune by Frank Herbert", p, "used") for p in (4.0, 6.0, 5.0)]


def test_new_is_min_and_used_is_median_with_sources():
    r = go(book(), FakeSource(new={"EBAY_US": NEW}, used={"EBAY_US": USED}))
    assert r.status == "priced" and r.replacement.amount == 12.5 and r.used.amount == 5.0 and r.used.count == 3
    assert r.replacement.url and r.replacement.retrieved_at and r.used.condition == CFG.condition_assumed


def test_no_listing_is_none_not_zero():
    r = go(book(), FakeSource())
    assert r.status == "unpriced" and r.replacement is None and r.used is None


def test_fx_conversion_flags_converted():
    src = FakeSource(new={"EBAY_US": [listing("Dune Frank Herbert", 10.0)]}, local=None)
    r = go(book(), src, cur="GBP", country="IN", fx=FakeFX({("USD", "GBP"): 0.8}))
    assert r.replacement.amount == 8.0 and r.replacement.converted and r.replacement.fx_rate == 0.8


def test_signed_and_antiquarian_and_expensive_never_priced():
    src = FakeSource(new={"EBAY_US": NEW}, used={"EBAY_US": USED})
    assert go(book(), src, [UserFact(ref="b_001", text="it is signed")]).status == "needs_appraisal"
    assert go(book(year=1850), src).reason == "antiquarian"
    big = FakeSource(new={"EBAY_US": [listing("Dune Frank Herbert", 500.0)]})
    r = go(book(), big)
    assert r.status == "needs_appraisal" and r.replacement is None


def test_few_used_listings_give_a_range_not_an_amount():
    r = go(book(), FakeSource(used={"EBAY_US": USED[:2]}))
    assert r.used.amount is None and (r.used.low, r.used.high) == (4.0, 6.0) and r.status == "range"


def test_wrong_book_and_lots_are_rejected():
    junk = [listing("Dune Messiah Frank Herbert", 3.0), listing("Dune Frank Herbert lot of 5", 2.0), listing("Gardening Basics", 1.0)]
    assert go(book(), FakeSource(new={"EBAY_US": junk})).status == "unpriced"


def test_items_art_and_unknown_model():
    art = Object(id="i_001", kind="item", sighting_ids=["s"], status="identified", frame_ref="f", bbox=(0, 0, 1, 1), category="art")
    src = FakeSource(new={"EBAY_US": [listing("Wall art print", p) for p in (10, 20, 30)]})
    assert price.price_item(art, [], "USD", "US", src, FakeFX(), CFG).status == "needs_appraisal"
    printed = price.price_item(art, [UserFact(ref="i_001", text="it's a print")], "USD", "US", src, FakeFX(), CFG)
    assert printed.status == "range" and printed.replacement.low < printed.replacement.high
    lamp = art.model_copy(update={"category": "lamp", "material": "brass"})
    assert price.price_item(lamp, [], "USD", "US", FakeSource(new={"EBAY_US": [listing("brass lamp", 10)]}), FakeFX(), CFG).status == "unpriced"  # <3 listings


def test_run_writes_prices(tmp_path):
    sd, _ = make_sweep(tmp_path)
    store.write(sd, "objects.json", [book()])
    price.run(sd, FakeSource(new={"EBAY_US": NEW}), FakeFX(), CFG)
    assert store.read(sd, "prices.json", list[PriceResult])[0].replacement.amount == 12.5
