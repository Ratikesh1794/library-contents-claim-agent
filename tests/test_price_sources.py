"""Google Books ebook prices as a real, retrievable price source; chained behind eBay. No network."""
import backend.adapters.google_books_prices as gbp
from backend.adapters.google_books_prices import GoogleBooksPrices
from backend.adapters.price_chain import ChainPriceSource
from backend.config import CFG
from backend.ports import Listing
from backend.schemas import Object
from backend.stages import price as price_stage
from tests.fakes import FakeFX


def vol(title, authors, amount, lang="en", sale="FOR_SALE", cur="USD"):
    return {"volumeInfo": {"title": title, "authors": authors, "language": lang, "infoLink": "https://books.google.com/info"},
            "saleInfo": {"saleability": sale, "retailPrice": {"amount": amount, "currencyCode": cur} if amount else None,
                         "buyLink": "https://play.google.com/buy"}}


def book(i, title, author):
    return Object(id=f"b_{i:03d}", kind="book", title=title, author=author, status="identified", frame_ref="frames/f.jpg",
                  sighting_ids=[], bbox=(0, 0, 1, 1))


def patch(monkeypatch, items):
    monkeypatch.setattr(gbp, "get_json", lambda ns, url, params, key_extra=None: ({"items": items}, "2026-10-17T10:00:00Z"))


ITEMS = [vol("Rich Dad Poor Dad - 20th Anniversary Edition", ["Robert Kiyosaki"], 7),
         vol("Rich Dad Poor Dad", ["Robert Kiyosaki"], 11.66, lang="kn"),                  # a Kannada edition
         vol("Full Summary of Rich Dad Poor Dad", ["MS. Prates"], 0.49),                    # a summary
         vol("Hero", ["Rhonda Byrne"], 14.99),                                              # another book
         vol("Rich Dad Poor Dad", ["Robert Kiyosaki"], None, sale="NOT_FOR_SALE")]          # not sold


def test_only_sold_english_volumes_with_a_price_are_listings_and_each_has_a_source_url_and_date(monkeypatch):
    patch(monkeypatch, ITEMS)
    got = GoogleBooksPrices().search("Rich Dad Poor Dad Robert Kiyosaki", "US", "new")
    assert [(l.title, l.price) for l in got] == [("Rich Dad Poor Dad - 20th Anniversary Edition Robert Kiyosaki", 7.0),
                                                 ("Full Summary of Rich Dad Poor Dad MS. Prates", 0.49), ("Hero Rhonda Byrne", 14.99)]
    assert all(l.url.startswith("https://play.google.com") and l.retrieved_at == "2026-10-17T10:00:00Z"
               and l.source == "google_books_ebook:US" and l.condition == "new" for l in got)


def test_no_used_prices_and_no_item_prices_from_this_source(monkeypatch):
    patch(monkeypatch, ITEMS)
    assert GoogleBooksPrices().search("x", "US", "used") == []
    assert GoogleBooksPrices().search("x", "US", "new", books=False) == []


def test_the_price_stage_accepts_only_the_right_book_and_labels_it_an_ebook_price(monkeypatch):
    patch(monkeypatch, ITEMS)
    o = book(1, "Rich Dad, Poor Dad", "Robert T. Kiyosaki")
    r = price_stage.price_book(o, [], "USD", "US", ChainPriceSource([GoogleBooksPrices()]), FakeFX(), CFG)
    assert r.status == "priced" and r.replacement.amount == 7.0 and r.used is None            # the summary, Kannada edition, Hero: rejected
    assert r.replacement.source == "google_books_ebook:US" and r.replacement.url and r.replacement.retrieved_at


def test_a_book_google_does_not_sell_stays_unpriced(monkeypatch):
    patch(monkeypatch, [vol("The LLM Advantage", ["Asish Dash"], 5.99)])
    o = book(2, "Mathematics for Machine Learning", "Marc Peter Deisenroth")
    r = price_stage.price_book(o, [], "USD", "US", ChainPriceSource([GoogleBooksPrices()]), FakeFX(), CFG)
    assert r.status == "unpriced" and r.reason == "no_price"


def test_another_country_gets_that_countrys_currency(monkeypatch):
    seen = {}
    def fake(ns, url, params, key_extra=None):
        seen["country"] = params["country"]
        return {"items": [vol("Rich Dad Poor Dad", ["Robert Kiyosaki"], 8.99, cur="GBP")]}, "2026-10-17T10:00:00Z"
    monkeypatch.setattr(gbp, "get_json", fake)
    o = book(3, "Rich Dad, Poor Dad", "Robert Kiyosaki")
    r = price_stage.price_book(o, [], "GBP", "GB", ChainPriceSource([GoogleBooksPrices()]), FakeFX({("USD", "GBP"): 0.8}), CFG)
    assert seen["country"] == "GB" and r.replacement.amount == 8.99 and r.replacement.currency == "GBP" and not r.replacement.converted


class Keyed:                                                          # stands in for eBay when its keys are set
    def local_market(self, c): return "EBAY_US"
    def fallback_markets(self): return ["EBAY_US"]
    def search(self, q, m, cond, books=True): return [Listing("Dune Frank Herbert", 9.0, "USD", "http://e/1", "2026-10-17T00:00:00Z", f"ebay:{m}", cond)]


def test_the_chain_prefers_the_first_source_and_falls_back_to_the_next(monkeypatch):
    patch(monkeypatch, [vol("Dune", ["Frank Herbert"], 7.99)])
    o = book(4, "Dune", "Frank Herbert")
    both = price_stage.price_book(o, [], "USD", "US", ChainPriceSource([Keyed(), GoogleBooksPrices()]), FakeFX(), CFG)
    assert both.replacement.source == "ebay:EBAY_US" and both.used.amount is not None or both.used is not None
    only_google = price_stage.price_book(o, [], "USD", "US", ChainPriceSource([GoogleBooksPrices()]), FakeFX(), CFG)
    assert only_google.replacement.source == "google_books_ebook:US"


def test_a_failed_query_loses_only_itself_and_the_same_listing_is_not_returned_twice(monkeypatch):
    calls = []
    def flaky(ns, url, params, key_extra=None):
        calls.append(params.get("filter"))
        if len(calls) == 1:
            raise TimeoutError("read timed out")
        return {"items": [vol("Dune", ["Frank Herbert"], 7.99)]}, "2026-10-17T10:00:00Z"
    monkeypatch.setattr(gbp, "get_json", flaky)
    got = GoogleBooksPrices().search("Dune Frank Herbert", "US", "new", hint={"title": "Dune", "author": "Frank Herbert"})
    assert len(calls) == 3 and [l.price for l in got] == [7.99]            # 3 query styles; one timed out; two returned the same book


def test_ebook_replacement_price_is_the_middle_listing_not_the_cheapest(monkeypatch):
    patch(monkeypatch, [vol("Dune", ["Frank Herbert"], p) for p in (0.49, 3.99, 12.99)])
    r = price_stage.price_book(book(7, "Dune", "Frank Herbert"), [], "USD", "US", ChainPriceSource([GoogleBooksPrices()]), FakeFX(), CFG)
    assert r.replacement.amount == 3.99                                    # not the $0.49 knock-off


def test_a_marketplace_price_is_still_the_lowest_accepted_new_listing():
    class Market:
        def local_market(self, c): return "EBAY_US"
        def fallback_markets(self): return ["EBAY_US"]
        def search(self, q, m, cond, books=True):
            return [Listing("Dune Frank Herbert", p, "USD", f"http://e/{p}", "2026-10-17T00:00:00Z", f"ebay:{m}", cond) for p in (14.0, 9.0, 11.0)]
    r = price_stage.price_book(book(8, "Dune", "Frank Herbert"), [], "USD", "US", Market(), FakeFX(), CFG)
    assert r.replacement.amount == 9.0


def test_the_same_books_can_be_priced_for_a_second_country(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, "scripts")
    from price_second_country import compare
    from backend.core import store
    sd = tmp_path / "s"
    sd.mkdir()
    store.write(sd, "meta.json", {"country": "US", "currency": "USD"})
    store.write(sd, "objects.json", [book(1, "Rich Dad, Poor Dad", "Robert Kiyosaki")])
    def fake(ns, url, params, key_extra=None):
        gbp_ = params["country"] == "GB"
        return {"items": [vol("Rich Dad Poor Dad", ["Robert Kiyosaki"], 8.99 if gbp_ else 7.0, cur="GBP" if gbp_ else "USD")]}, "2026-10-17T10:00:00Z"
    monkeypatch.setattr(gbp, "get_json", fake)
    rows = compare(sd, "GB", "GBP", source=ChainPriceSource([GoogleBooksPrices()]), fx=FakeFX({("USD", "GBP"): 0.8}))
    assert rows[0]["home"]["amount"] == 7.0 and rows[0]["home"]["currency"] == "USD"
    assert rows[0]["second"]["amount"] == 8.99 and rows[0]["second"]["currency"] == "GBP" and rows[0]["second"]["converted"] is False
