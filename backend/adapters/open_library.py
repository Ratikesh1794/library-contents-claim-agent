"""Open Library search -> Candidates. Free, no key. Work-level data: `first_publish_year` is
the WORK's year, so it is deliberately not used as an edition year."""
from __future__ import annotations

from backend.core.http import get_json
from backend.schemas import Candidate


class OpenLibrary:
    URL = "https://openlibrary.org/search.json"

    def search(self, title: str, author: str | None) -> list[Candidate]:
        params = {"title": title, "limit": 10,
                  "fields": "key,title,author_name,isbn,publisher"}
        if author:
            params["author"] = author
        data, _ = get_json("openlibrary", self.URL, params)
        out = []
        for d in data.get("docs", []):
            out.append(Candidate(
                title=d.get("title", ""), author=(d.get("author_name") or [None])[0],
                isbn=(d.get("isbn") or [None])[0], publishers=(d.get("publisher") or [])[:20],
                source_url=f"https://openlibrary.org{d.get('key', '')}"))
        return out
