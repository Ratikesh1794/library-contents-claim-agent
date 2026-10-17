"""Serper Google Shopping prices: parsing, new/used, author check, subtitles, source order. No network."""
import backend.adapters.serper_prices as sp
from backend.adapters.google_books_prices import GoogleBooksPrices
from backend.adapters.price_chain import ChainPriceSource
from backend.adapters.serper_prices import SerperPrices, is_used, parse_price
from backend.config import CFG
from backend.ports import Listing
from backend.stages import price as price_stage
from tests.fakes import FakeFX
from tests.test_price_sources import book


def test_prices_are_parsed_and_a_currency_that_cannot_be_read_is_never_guessed():
    assert parse_price("$1,299.00", "US") == (1299.0, "USD")
    assert parse_price("£8.99", "GB") == (8.99, "GBP")
    assert parse_price("€ 1.299,00", "DE") == (1299.0, "EUR")
    assert parse_price("€12,50", "FR") == (12.5, "EUR")
    assert parse_price("$12", "CA") == (12.0, "CAD") and parse_price("$12", "AU") == (12.0, "AUD")
    assert parse_price("CA$ 12.50", "US") == (12.5, "CAD")
    assert parse_price("12.50", "US") is None and parse_price("", "US") is None and parse_price("Free", "US") is None


def test_used_only_on_clear_evidence():
    assert is_used("Dune (Used - Good)", "Amazon") and is_used("Dune", "eBay") and is_used("iPhone Refurbished", "Walmart")
    assert not is_used("Dune Frank Herbert Paperback", "Barnes & Noble") and not is_used("Dune", "Amazon.com")


SHOP = {"shopping": [
    {"title": "The Power of Now: A Guide to Spiritual Enlightenment by Eckhart Tolle", "source": "Barnes & Noble", "price": "$12.99", "link": "https://bn.example/1"},
    {"title": "The Power of Now Eckhart Tolle (Used - Good)", "source": "ThriftBooks", "price": "$4.50", "link": "https://tb.example/1"},
    {"title": "The Power of Now Eckhart Tolle", "source": "Amazon.com", "price": "price on request", "link": "https://a.example/1"},
    {"title": "The Power of Now Eckhart Tolle", "source": "NoLink", "price": "$9.99", "link": ""}]}


def patch(monkeypatch, payload=SHOP):
    monkeypatch.setattr(sp, "post_json", lambda ns, url, body, headers=None, key_extra=None: (payload, "2026-10-17T12:00:00Z"))
    monkeypatch.setenv("SERPER_API_KEY", "test-key-not-real")


def test_new_and_used_are_split_and_each_listing_has_merchant_link_and_date(monkeypatch):
    patch(monkeypatch)
    new = SerperPrices().search("The Power of Now Eckhart Tolle", "US", "new")
    used = SerperPrices().search("The Power of Now Eckhart Tolle", "US", "used")
    assert [(l.price, l.currency, l.url) for l in new] == [(12.99, "USD", "https://bn.example/1")]       # unreadable price, no link: skipped
    assert [(l.price, l.url) for l in used] == [(4.5, "https://tb.example/1")]
    assert new[0].source == "serper_shopping:Barnes & Noble:US" and new[0].retrieved_at == "2026-10-17T12:00:00Z" and new[0].verify_author


def test_no_key_means_no_listings_and_the_source_is_not_configured(monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    assert SerperPrices().search("x", "US", "new") == [] and not SerperPrices.configured()


def test_the_price_stage_accepts_a_subtitle_but_needs_the_author_and_still_rejects_other_titles(monkeypatch):
    o = book(1, "The Power of Now", "Eckhart Tolle")
    ok = lambda title: price_stage._accept_book(o, Listing(title, 9.0, "USD", "u", "t", "serper", "new", verify_author=True), CFG)
    assert ok("The Power of Now: A Guide to Spiritual Enlightenment by Eckhart Tolle")
    assert ok("The Power of Now (Paperback) Eckhart Tolle")
    assert not ok("The Power of Now: A Guide to Spiritual Enlightenment")                  # no author in the title: 'The Power of Now' is not unique
    assert not ok("Practicing the Power of Now Eckhart Tolle")                             # a different book
    assert not ok("The Power of Now Workbook Eckhart Tolle")                               # blocked: a companion
    d = book(2, "Dune", "Frank Herbert")
    assert not price_stage._accept_book(d, Listing("Dune Messiah Frank Herbert", 9.0, "USD", "u", "t", "s", "new"), CFG)
    assert price_stage._accept_book(d, Listing("Dune: A Novel Frank Herbert", 9.0, "USD", "u", "t", "s", "new"), CFG)


def test_a_shared_title_by_another_author_is_not_priced(monkeypatch):
    patch(monkeypatch, {"shopping": [{"title": "The Secret by Lucinda Riley (Paperback)", "source": "Amazon.com", "price": "$9.99", "link": "https://a/1"},
                                     {"title": "The Secret by Rhonda Byrne (Hardcover)", "source": "Amazon.com", "price": "$14.99", "link": "https://a/2"}]})
    r = price_stage.price_book(book(3, "The Secret", "Rhonda Byrne"), [], "USD", "US", ChainPriceSource([SerperPrices()]), FakeFX(), CFG)
    assert r.status == "priced" and r.replacement.amount == 14.99 and r.replacement.url == "https://a/2"


def test_serper_comes_before_google_books_and_each_countrys_own_market_is_tried_first(monkeypatch):
    chain = ChainPriceSource([SerperPrices(), GoogleBooksPrices()])
    assert chain.local_market("GB") == "0|GB"
    assert chain.fallback_markets() == ["0|GB", "1|GB", "0|US", "1|US"]        # local for every source, then the foreign ones
