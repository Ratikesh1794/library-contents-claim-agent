"""ECB reference FX via Frankfurter (free, no key). Rate + date are stored on converted prices."""
from __future__ import annotations

import time

from backend.core.http import get_json


class Frankfurter:
    URL = "https://api.frankfurter.dev/v1/latest"

    def rate(self, base: str, quote: str) -> tuple[float, str]:
        if base == quote:
            return 1.0, time.strftime("%Y-%m-%d")
        data, _ = get_json("fx", self.URL, {"base": base, "symbols": quote},
                           key_extra=[time.strftime("%Y-%m-%d")])    # refresh daily
        return float(data["rates"][quote]), data["date"]
