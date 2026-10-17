"""PRICE stage: objects.json + user_facts.json + meta.json  ->  prices.json

Rules (design D6, NFR-I1): a number exists only if it comes from a retrieved listing with
source + URL + retrieved_at. The model never produces a price. No listing => None, excluded from totals.

Books:
  * pre-flags -> needs_appraisal (never priced): user said signed/inscribed; edition year < antiquarian_year
  * replacement = lowest accepted NEW listing; used = median of accepted USED listings (>= used_min_listings),
    fewer => a low/high range, no amount
  * local marketplace first; if it has nothing, fallback markets, converted with FX (`converted=true`)
  * post-flags -> needs_appraisal: any price above appraisal_threshold, or used prices diverge > diverge_ratio
Items: brand/model known => query the model; unknown => category+material, 20th/80th percentile range;
art => needs_appraisal unless the user said it is a print.

TO UPGRADE: change sources in deps.price_source(); add an LLM yes/no listing judge inside `_accept`.
"""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path
from statistics import median

import numpy as np

from backend import deps
from backend.core import facts, store
from backend.stages.catalogue import author_surname, norm, token_set_ratio
from backend.config import CFG, Config
from backend.ports import FX, Listing, PriceSource
from backend.schemas import Object, Price, PriceResult, UserFact

log = logging.getLogger(__name__)


def _blocked(title: str, own: str, cfg: Config) -> bool:
    t, o = norm(title), norm(own)
    return any(w in t and w not in o for w in cfg.listing_blocklist)


STOP = {"the", "a", "an", "of", "and", "in", "on", "to", "for", "with", "my", "your", "is", "it"}


def _distinctive(title: str | None) -> bool:
    """Two or more significant words ('The Power of Now', 'Shoe Dog'). A shorter title ('The Secret', 'Dune', 'It') is
    shared by many books, so a listing without an author field must name the author to count. (Merchant titles mostly
    omit the author: requiring it always would reject nearly every listing.)"""
    main = re.split(r"\s*[:;(]\s*", title or "", maxsplit=1)[0]
    return len([w for w in norm(main).split() if w not in STOP]) >= 2


def _accept_book(o: Object, l: Listing, cfg: Config) -> bool:
    """ISBN in the listing title, or: after removing author + format words, what is left must match the title closely
    (so 'Dune' does NOT accept 'Dune Messiah'). A listing may also carry a subtitle ('The Power of Now: A Guide to...',
    'The Secret by Rhonda Byrne'): the part before it is compared too. A source with no author field
    (`verify_author`) must show the author's surname in the listing title: 'The Secret' is the title of many books."""
    if _blocked(l.title, o.title or "", cfg):
        return False
    if o.isbn and o.isbn.replace("-", "") in l.title.replace("-", ""):
        return True
    if l.verify_author and not _distinctive(o.title) and (not o.author or author_surname(o.author) not in norm(l.title).split()):
        return False
    drop = set(norm(o.author).split()) | set(cfg.listing_noise) - set(norm(o.title).split())
    rest = lambda t: " ".join(w for w in norm(t).split() if w not in drop and not re.fullmatch(r"\d+(st|nd|rd|th)", w))
    main = re.split(r"\s*[:|(\[]\s*|\s+-\s+|\s+by\s+", l.title, maxsplit=1)[0]
    return max(SequenceMatcher(None, norm(o.title), rest(t)).ratio() for t in (l.title, main)) >= cfg.list_T


def _convert(l: Listing, currency: str, fx: FX) -> tuple[float, bool, float | None, str | None]:
    if l.currency == currency:
        return l.price, False, None, None
    rate, date = fx.rate(l.currency, currency)
    return round(l.price * rate, 2), True, rate, date


def _gather(query: str, cond: str, local: str | None, src: PriceSource, accept, currency: str, fx: FX, hint: dict | None = None):
    """-> list of (listing, converted_amount, converted, rate, date) from the first market that has accepted ones.
    `hint` ({title, author}) is passed only to sources that ask for it (`wants_hints`): a structured search is far more
    exact than free text, which returns summaries, translations and other books by the same author."""
    kw = {"hint": hint} if hint and getattr(src, "wants_hints", False) else {}
    for market in ([local] if local else []) + src.fallback_markets():
        try:
            ls = [l for l in src.search(query, market, cond, **kw) if accept(l)]
        except Exception as e:                  # NFR-E5
            log.warning("price source failed (%s): %s", market, e)
            continue
        if ls:
            return [(l, *_convert(l, currency, fx)) for l in ls]
    return []


def _mk(kind, rows, currency, cond, amount=None, low=None, high=None, rep=None) -> Price:
    l, _, conv, rate, date = rep
    return Price(kind=kind, amount=amount, low=low, high=high, currency=currency, source=l.source, url=l.url,
                 retrieved_at=l.retrieved_at, condition=cond, count=len(rows), converted=conv, fx_rate=rate, fx_date=date)


def price_book(o: Object, user_facts: list[UserFact], currency: str, country: str | None,
               src: PriceSource, fx: FX, cfg: Config = CFG) -> PriceResult:
    """Replacement (new) and used value for one book, from retrieved listings only.
    Signed or antiquarian books, used prices that disagree too much, or any price above the appraisal threshold
    -> needs_appraisal with no number. New = lowest accepted listing (median for ebook stores); used = median of
    `used_min_listings`+ listings, else a low-high range. Foreign listings are converted and marked."""
    f = facts.flags(o, user_facts)
    if "signed" in f:
        return PriceResult(object_id=o.id, status="needs_appraisal", reason="signed_or_inscribed")
    if o.year and o.year < cfg.antiquarian_year:
        return PriceResult(object_id=o.id, status="needs_appraisal", reason="antiquarian")
    if not o.title:
        return PriceResult(object_id=o.id, status="unpriced", reason="unidentified")
    query = o.isbn or f"{o.title} {o.author or ''}".strip()
    local = src.local_market(country)
    acc = lambda l: _accept_book(o, l, cfg)
    hint = {"title": o.title, "author": o.author}
    new = _gather(query, "new", local, src, acc, currency, fx, hint)
    used = _gather(query, "used", local, src, acc, currency, fx, hint)

    repl = None
    if new:
        pool = sorted(new, key=lambda r: r[1])
        # Lowest accepted NEW listing, except for ebook prices: an ebook store lists cheap knock-off editions next to the
        # real one ($0.49 for a "$12" book), so the middle of the accepted ones is the honest figure there.
        rep = pool[len(pool) // 2] if pool[0][0].source.startswith("google_books_ebook") else pool[0]
        repl = _mk("replacement", new, currency, "new", amount=rep[1], rep=rep)
    usd = None
    if used:
        vals = sorted(r[1] for r in used)
        rep = min(used, key=lambda r: abs(r[1] - median(vals)))
        if len(vals) >= cfg.used_min_listings:
            usd = _mk("used", used, currency, cfg.condition_assumed, amount=round(median(vals), 2), rep=rep)
        else:
            usd = _mk("used", used, currency, cfg.condition_assumed, low=vals[0], high=vals[-1], rep=rep)
        # Rare/collectible copies show as prices that genuinely disagree. With many listings compare the typical spread
        # (20th..80th percentile): among 20 used copies of a popular book one collector's listing always exists, and
        # cheapest-vs-dearest flagged ordinary paperbacks for appraisal.
        lo_q, hi_q = (float(np.percentile(vals, 20)), float(np.percentile(vals, 80))) if len(vals) >= 5 else (vals[0], vals[-1])
        if hi_q / max(lo_q, 0.01) > cfg.diverge_ratio:
            return PriceResult(object_id=o.id, status="needs_appraisal", reason="used_prices_diverge")
    limit = cfg.appraisal_threshold_usd * (fx.rate("USD", currency)[0] if currency != "USD" else 1.0)
    if any(v is not None and v > limit for p in (repl, usd) if p for v in (p.amount, p.high)):
        return PriceResult(object_id=o.id, status="needs_appraisal", reason="above_threshold")
    if not repl and not usd:
        return PriceResult(object_id=o.id, status="unpriced", reason="no_price")
    status = "priced" if (repl and repl.amount is not None) or (usd and usd.amount is not None) else "range"
    return PriceResult(object_id=o.id, status=status, replacement=repl, used=usd)


def price_item(o: Object, user_facts: list[UserFact], currency: str, country: str | None,
               src: PriceSource, fx: FX, cfg: Config = CFG) -> PriceResult:
    """Replacement cost for one non-book item. Art needs appraisal unless the user said it is a print.
    A model number (brand_model with a digit) -> one listing, the one nearest the median (`priced`).
    Otherwise search 'brand + name' and give the 25th-75th percentile of 3+ listings (`range`), with
    reason `wide_range` when that range is wider than `diverge_ratio`."""
    f = facts.flags(o, user_facts)
    if o.category == "art" and "print" not in f:
        return PriceResult(object_id=o.id, status="needs_appraisal", reason="art_without_print_confirmation")
    if not o.category:
        return PriceResult(object_id=o.id, status="unpriced", reason="unidentified")
    # A model has a number in it ("Sony WH-1000XM4"). A brand alone ("M-Audio") is not a model: searching it prices
    # whatever that brand sells, so it only narrows the item search ("M-Audio audio amplifier") and gives a range.
    known = bool(o.brand_model) and any(ch.isdigit() for ch in o.brand_model)
    # Search by what the item is ("office chair"), not by its category ("furniture" returns any furniture at all).
    brand = o.brand_model if o.brand_model and not known else None
    query: str = (o.brand_model if known else None) or " ".join(x for x in (brand, o.name) if x) or " ".join(x for x in (o.material, "art print decor" if o.category == "art" else o.category) if x)
    local = src.local_market(country)
    acc = (lambda l: token_set_ratio(o.brand_model, l.title) >= cfg.list_T and not _blocked(l.title, "", cfg)) if known \
        else (lambda l: not _blocked(l.title, "", cfg))
    rows = _gather(query, "new", local, _AnyCategory(src), acc, currency, fx)
    if not rows:
        return PriceResult(object_id=o.id, status="unpriced", reason="no_price")
    amts = sorted(r[1] for r in rows)
    if known:                                   # single source => low == high (stated in `source`)
        rep = min(rows, key=lambda r: abs(r[1] - median(amts)))     # the typical listing, not the cheapest odd one
        p = _mk("replacement", rows, currency, "new", low=amts[0], high=amts[0], rep=rep)
        return PriceResult(object_id=o.id, status="priced", replacement=p)
    if len(amts) < 3:
        return PriceResult(object_id=o.id, status="unpriced", reason="too_few_listings")
    lo, hi = (round(float(np.percentile(amts, q)), 2) for q in cfg.item_range_pct)
    rep = min(rows, key=lambda r: abs(r[1] - median(amts)))
    # "floor speaker" matches a $600 speaker and a $22,000 one: the range is sourced but tells an adjuster little.
    wide = "wide_range" if hi / max(lo, 0.01) > cfg.diverge_ratio else None
    return PriceResult(object_id=o.id, status="range", reason=wide,
                       replacement=_mk("replacement", rows, currency, "new", low=lo, high=hi, rep=rep))


class _AnyCategory(PriceSource):
    """Wrap a PriceSource so item searches are not restricted to the books category (if the source supports it)."""

    def __init__(self, src: PriceSource):
        self.src = src

    def search(self, query: str, market: str, condition: str) -> list[Listing]:
        try:
            return self.src.search(query, market, condition, books=False)   # type: ignore[call-arg]
        except TypeError:
            return self.src.search(query, market, condition)

    def local_market(self, country: str | None) -> str | None:
        return self.src.local_market(country)

    def fallback_markets(self) -> list[str]:
        return self.src.fallback_markets()


def run(sd: Path, source: PriceSource | None = None, fx: FX | None = None,
        cfg: Config = CFG, deadline: float | None = None) -> None:
    """Price every object of the sweep in parallel and write prices.json. Country and currency come from meta.json
    (the user's answer). A failure prices that line as unpriced/price_error; past `deadline` the stage is marked incomplete."""
    sd = Path(sd)
    src: PriceSource = source or deps.price_source()
    rates: FX = fx or deps.fx()
    meta = store.read(sd, "meta.json")
    objs = store.read(sd, "objects.json", list[Object], default=[])
    user_facts: list[UserFact] = store.read(sd, "user_facts.json", list[UserFact], default=[])
    cur, country = meta["currency"], meta.get("country")
    timed_out = []

    def work(o: Object):
        if deadline and time.time() > deadline:
            timed_out.append(o.id)
            return None
        try:
            fn = price_book if o.kind == "book" else price_item
            return fn(o, user_facts, cur, country, src, rates, cfg)
        except Exception as e:                  # NFR-E5: one failure => blank line + review, not a crash
            log.warning("pricing %s failed: %s", o.id, e)
            return PriceResult(object_id=o.id, status="unpriced", reason="price_error")

    with ThreadPoolExecutor(cfg.price_workers) as ex:
        results = [r for r in ex.map(work, objs) if r]
    store.write(sd, "prices.json", results)
    (store.mark_incomplete if timed_out else store.clear_incomplete)(sd, "price")
