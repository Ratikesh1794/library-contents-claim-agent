"""The policyholder's country and claim currency. Always from the user (start screen or voice), never a default."""
from __future__ import annotations

import re

_CODE = {2: re.compile(r"^[A-Z]{2}$"), 3: re.compile(r"^[A-Z]{3}$")}


def clean(country, currency) -> tuple[str | None, str | None]:
    """'gb', ' gbp ' -> ('GB', 'GBP'); anything that is not an ISO-shaped code -> None (the agent then asks)."""
    c = str(country or "").strip().upper().replace("UK", "GB")
    cur = str(currency or "").strip().upper()
    return (c if _CODE[2].match(c) else None), (cur if _CODE[3].match(cur) else None)


if __name__ == "__main__":
    assert clean("gb", " gbp ") == ("GB", "GBP") and clean("uk", "eur") == ("GB", "EUR")
    assert clean(None, "") == (None, None) and clean("United States", "dollars") == (None, None)
