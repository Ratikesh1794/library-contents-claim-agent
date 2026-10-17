"""SerpApi Google Shopping prices (shape taken from a live response), the short-title author rule, and item names."""
import backend.adapters.serpapi_prices as sa
from backend.adapters.google_books_prices import GoogleBooksPrices
from backend.adapters.price_chain import ChainPriceSource
from backend.adapters.serpapi_prices import SerpApiPrices, parse_price
from backend.config import CFG
from backend.ports import Listing
from backend.schemas import Object
from backend.stages import price as price_stage
from tests.fakes import FakeFX
from tests.test_price_sources import book


def r(title, price, merchant, extracted=None, used=None):
    d = {"title": title, "price": price, "source": merchant, "product_link": f"https://www.google.com/shopping/product/{abs(hash(title + price))}"}
    if extracted is not None:
        d["extracted_price"] = extracted
    if used:
        d["second_hand_condition"] = used
    return d


LIVE_LIKE = {"shopping_results": [                                   # as returned for "The Power of Now Eckhart Tolle book"
    r("The Power of Now : A Guide to Spiritual Enlightenment", "$4.41", "AbeBooks.com", 4.41, "pre-owned"),
    r("The Power of Now: A Guide to Spiritual Enlightenment", "$6.77", "AbeBooks.com", 6.77, "pre-owned"),
    r("The Power of Now: A Guide to Spiritual Enlightenment [Paperback]", "$6.95", "Walmart - Wob", 6.95, "pre-owned"),
    r("Practicing The Power Of Now - Eckhart Tolle", "$9.05", "AbeBooks.com", 9.05, "pre-owned"),
    r("The Power of Now by Eckhart Tolle", "$8.87", "eBay - nextgen-deals", 8.87),
    r("The Power of Now: A Guide to Spiritual Enlightenment", "$14.99", "Barnes & Noble", 14.99),
    r("The Power of Now", "price on request", "Somewhere")]}


def patch(monkeypatch, payload=LIVE_LIKE):
    calls = []
    def fake(ns, url, params, key_extra=None, timeout=None):
        calls.append(params["q"])
        return payload, "2026-10-18T09:00:00Z"
    monkeypatch.setattr(sa, "get_json", fake)
    monkeypatch.setenv("SERPAPI_API_KEY", "not-a-real-key")
    return calls


def test_new_and_used_come_from_googles_own_condition_field(monkeypatch):
    patch(monkeypatch)
    new = SerpApiPrices().search("The Power of Now Eckhart Tolle", "US", "new")
    used = SerpApiPrices().search("The Power of Now Eckhart Tolle", "US", "used")
    assert sorted(l.price for l in new) == [8.87, 14.99] and sorted(l.price for l in used) == [4.41, 6.77, 6.95, 9.05]
    assert all(l.url.startswith("https://www.google.com/shopping/product/") and l.retrieved_at == "2026-10-18T09:00:00Z" for l in new + used)
    assert {l.source for l in new} == {"google_shopping:eBay - nextgen-deals:US", "google_shopping:Barnes & Noble:US"}


def test_a_book_gets_a_replacement_and_a_used_value_from_real_listings(monkeypatch):
    patch(monkeypatch)
    res = price_stage.price_book(book(1, "The Power of Now", "Eckhart Tolle"), [], "USD", "US", ChainPriceSource([SerpApiPrices()]), FakeFX(), CFG)
    assert res.status == "priced" and res.replacement.amount == 8.87                        # lowest accepted new listing
    assert res.used.amount == 6.77 and res.used.count == 3                                  # median of 3 pre-owned; "Practicing..." rejected


def test_short_titles_shared_by_many_books_need_the_author_long_ones_do_not():
    assert not price_stage._distinctive("The Secret") and not price_stage._distinctive("Dune") and not price_stage._distinctive("It")
    assert price_stage._distinctive("The Power of Now") and price_stage._distinctive("Shoe Dog: A Memoir")
    mk = lambda t: Listing(t, 9.0, "USD", "u", "t", "google_shopping", "new", verify_author=True)
    secret = book(2, "The Secret", "Rhonda Byrne")
    assert not price_stage._accept_book(secret, mk("The Secret (Paperback)"), CFG)          # which "The Secret"?
    assert price_stage._accept_book(secret, mk("The Secret by Rhonda Byrne (Hardcover)"), CFG)
    assert price_stage._accept_book(book(3, "The Power of Now", "Eckhart Tolle"), mk("The Power of Now: A Guide to Spiritual Enlightenment"), CFG)


def test_one_search_per_query_and_country_new_and_used_share_it(monkeypatch):
    calls = patch(monkeypatch)
    SerpApiPrices().search("Dune Frank Herbert", "US", "new")
    SerpApiPrices().search("Dune Frank Herbert", "US", "used")
    assert calls == ["Dune Frank Herbert book", "Dune Frank Herbert book"]   # the same request: the on-disk cache answers the 2nd


def test_unreadable_currency_is_skipped_and_parsing_is_careful():
    assert parse_price("$1,299.00", "US") == (1299.0, "USD") and parse_price("£8.99", "GB") == (8.99, "GBP")
    assert parse_price("€ 1.299,00", "DE") == (1299.0, "EUR") and parse_price("$12", "CA") == (12.0, "CAD")
    assert parse_price("12.50", "US") is None and parse_price("price on request", "US") is None


def test_items_are_searched_by_what_they_are_and_get_a_sourced_range(monkeypatch):
    calls = patch(monkeypatch, {"shopping_results": [r(f"Mesh Office Chair {i}", f"${p}", "Store", p) for i, p in enumerate((89, 120, 149, 199, 260))]})
    o = Object(id="i_001", kind="item", category="furniture", name="office chair", material="mesh and plastic", status="identified",
               frame_ref="frames/f.jpg", sighting_ids=[], bbox=(0, 0, 1, 1))
    res = price_stage.price_item(o, [], "USD", "US", ChainPriceSource([SerpApiPrices(), GoogleBooksPrices()]), FakeFX(), CFG)
    assert calls[0] == "office chair"                                                         # not "mesh and plastic furniture"
    assert res.status == "range" and 89 < res.replacement.low < res.replacement.high < 260 and res.replacement.url


def test_the_item_name_reaches_the_packet_description():
    from backend.schemas import ItemGuess, ItemLine
    from backend.stages.dedupe import build_objects
    from tests.test_dedupe import fr, item, shift
    ss = [item(0, 100, "furniture"), item(1, 100, "furniture")]
    for s in ss:
        s.item = ItemGuess(name="office chair", category="furniture", material="mesh")
    objs = [o for o in build_objects(ss, [fr(0), fr(1)], {("f_0000", "f_0001"): shift(0)}) if o.kind == "item"]
    assert objs[0].name == "office chair"
    assert "description" in ItemLine.model_fields


def test_audiobook_and_digital_merchants_are_not_book_prices(monkeypatch):
    import backend.adapters.serpapi_prices as sp
    rows = [{"title": "The Power of Now", "price": "$2.50", "extracted_price": 2.5, "product_link": "u1", "source": "Audiobooks.com"},
            {"title": "The Power of Now", "price": "$12.00", "extracted_price": 12.0, "product_link": "u2", "source": "Target"}]
    monkeypatch.setenv("SERPAPI_API_KEY", "k")
    monkeypatch.setattr(sp, "get_json", lambda *a, **k: ({"shopping_results": rows}, "2026-10-18T00:00:00Z"))
    got = sp.SerpApiPrices().search("The Power of Now Eckhart Tolle", "US", "new")
    assert [l.price for l in got] == [12.0]
