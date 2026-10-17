"""Price the SAME identified books for a second country: proof that locale is a setting, not hardcoded.

    python scripts/price_second_country.py sweeps/<id> GB            # currency GBP
    python scripts/price_second_country.py sweeps/<id> DE EUR        # or name the currency yourself

Re-runs the price stage's own book pricing (same source chain, same strict listing checks) for the other country and
prints both side by side with each price's source and retrieval date. Nothing is written to the sweep.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from backend import deps                                   # noqa: E402
from backend.core import store                             # noqa: E402
from backend.config import CFG                             # noqa: E402
from backend.schemas import Object, UserFact               # noqa: E402
from backend.stages import price                           # noqa: E402

CURRENCY = {"US": "USD", "GB": "GBP", "UK": "GBP", "DE": "EUR", "FR": "EUR", "IT": "EUR", "ES": "EUR", "CA": "CAD", "AU": "AUD", "IN": "INR"}


def compare(sd: Path, country: str, currency: str, limit: int = 10, source=None, fx=None) -> list[dict]:
    sd = Path(sd)
    meta = store.read(sd, "meta.json", dict, default={})
    objs = [o for o in store.read(sd, "objects.json", list[Object], default=[]) if o.kind == "book" and o.title][:limit]
    facts = store.read(sd, "user_facts.json", list[UserFact], default=[])
    source, fx = source or deps.price_source(), fx or deps.fx()
    home_c, home_cur = meta.get("country"), meta.get("currency") or "USD"
    rows = []
    for o in objs:
        row = {"title": o.title}
        for label, c, cur in (("home", home_c, home_cur), ("second", country, currency)):
            r = price.price_book(o, facts, cur, c, source, fx, CFG)
            p = r.replacement
            row[label] = {"country": c, "currency": cur, "status": r.status, "amount": p.amount if p else None,
                          "source": p.source if p else None, "retrieved_at": p.retrieved_at if p else None,
                          "converted": p.converted if p else None, "url": p.url if p else None}
        rows.append(row)
    return rows


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
    sweep, country = Path(sys.argv[1]), sys.argv[2].upper()
    currency = (sys.argv[3] if len(sys.argv) > 3 else CURRENCY.get(country, "USD")).upper()
    rows = compare(sweep, country, currency)
    fmt = lambda c: f"{c['amount']:>8} {c['currency']}" + (" (converted)" if c["converted"] else "") if c["amount"] is not None else f"{'-':>8} {c['status']}"
    print(f"{'book':<40}{rows[0]['home']['country'] if rows else 'home':>4} {'':>14}  {country:>4}")
    for r in rows:
        print(f"{r['title'][:38]:<40}{fmt(r['home']):<26}{fmt(r['second'])}   {r['second']['source'] or ''}")
    (sweep / f"prices_{country}.json").write_text(json.dumps(rows, indent=2))
    print(f"\nwritten: {sweep / f'prices_{country}.json'}")
