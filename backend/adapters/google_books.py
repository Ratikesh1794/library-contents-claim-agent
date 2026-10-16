"""Google Books volumes search -> Candidates (fallback catalogue). Key optional (quota only)."""
from __future__ import annotations

import os

from backend.core.http import get_json
from backend.schemas import Candidate


class GoogleBooks:
    URL = "https://www.googleapis.com/books/v1/volumes"

    def search(self, title: str, author: str | None) -> list[Candidate]:
        q = f"intitle:{title}" + (f"+inauthor:{author}" if author else "")
        params = {"q": q, "maxResults": 10}
        if os.environ.get("GOOGLE_BOOKS_API_KEY"):
            params["key"] = os.environ["GOOGLE_BOOKS_API_KEY"]
        data, _ = get_json("googlebooks", self.URL, params, key_extra=["v1"])
        out = []
        for it in data.get("items", []):
            v = it.get("volumeInfo", {})
            ids = {i.get("type"): i.get("identifier") for i in v.get("industryIdentifiers", [])}
            year = (v.get("publishedDate") or "")[:4]
            out.append(Candidate(
                title=v.get("title", ""), author=(v.get("authors") or [None])[0],
                isbn=ids.get("ISBN_13") or ids.get("ISBN_10"),
                publishers=[v["publisher"]] if v.get("publisher") else [],
                year=int(year) if year.isdigit() else None, edition_level=True,
                source_url=v.get("infoLink") or it.get("selfLink", "")))
        return out
