"""Open Library source."""

from __future__ import annotations

import json
import logging

from ..http import FetchError
from ..record import Confidence
from .base import Candidate, Source

LOG = logging.getLogger("blmeta.source.openlibrary")


class OpenLibrary(Source):
    name = "openlibrary"
    confidence = Confidence.MEDIUM

    def search(self, isbn13: str) -> list[Candidate]:
        url = (
            "https://openlibrary.org/api/books?bibkeys=ISBN:"
            f"{isbn13}&format=json&jscmd=data"
        )
        try:
            response = self.fetcher.get(url, accept="application/json")
        except FetchError as exc:
            LOG.info("open library unavailable: %s", exc)
            raise

        if response.status == 404 or not response.body.strip():
            return []
        try:
            payload = json.loads(response.body)
        except json.JSONDecodeError:
            return []

        entry = payload.get(f"ISBN:{isbn13}")
        if not entry:
            return []

        data: dict[str, object] = {}
        if entry.get("title"):
            data["title"] = entry["title"]
        if entry.get("subtitle"):
            data["subtitle"] = entry["subtitle"]

        authors = [a.get("name", "") for a in entry.get("authors", []) if a.get("name")]
        if authors:
            data["author"] = authors[0]
            if len(authors) > 1:
                data["contributors"] = authors[1:]

        publishers = [p.get("name", "") for p in entry.get("publishers", []) if p.get("name")]
        if publishers:
            data["publisher"] = publishers[0]

        places = [p.get("name", "") for p in entry.get("publish_places", []) if p.get("name")]
        if places:
            data["publication_place"] = places[0]

        if entry.get("publish_date"):
            data["publication_date"] = entry["publish_date"]
        if entry.get("number_of_pages"):
            data["page_count"] = str(entry["number_of_pages"])

        subjects = [s.get("name", "") for s in entry.get("subjects", []) if s.get("name")]
        if subjects:
            data["subjects"] = subjects

        cover = entry.get("cover") or {}
        if cover.get("large") or cover.get("medium"):
            data["cover_url"] = cover.get("large") or cover.get("medium")

        identifiers = entry.get("identifiers", {}) or {}
        extra: list[str] = []
        for scheme, values in identifiers.items():
            if scheme in ("isbn_10", "isbn_13"):
                continue
            for value in values:
                extra.append(f"{scheme}:{value}")
        if extra:
            data["other_identifiers"] = extra
        if identifiers.get("oclc"):
            data["oclc_number"] = identifiers["oclc"][0]

        isbns = set()
        for key in ("isbn_10", "isbn_13"):
            for value in identifiers.get(key, []):
                isbns.add(value)
        # The API is keyed by our query, so the queried ISBN is implicitly the
        # record's -- but only trust it if the payload itself is non-empty.
        isbns.add(isbn13)

        return [
            Candidate(
                isbns=isbns,
                data=data,
                record_id=entry.get("key", ""),
                raw=json.dumps(entry, indent=2),
                raw_format="json",
            )
        ]


class GoogleBooks(Source):
    """Google Books. Aggressively rate-limited; best-effort only."""

    name = "googlebooks"
    confidence = Confidence.MEDIUM

    def search(self, isbn13: str) -> list[Candidate]:
        url = f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn13}"
        try:
            response = self.fetcher.get(url, accept="application/json")
        except FetchError as exc:
            LOG.info("google books unavailable: %s", exc)
            raise

        try:
            payload = json.loads(response.body)
        except json.JSONDecodeError:
            return []

        if "error" in payload:
            raise FetchError(f"google books: {payload['error'].get('message', 'error')}")

        candidates: list[Candidate] = []
        for item in payload.get("items", []):
            info = item.get("volumeInfo", {})
            isbns = {
                ident.get("identifier", "")
                for ident in info.get("industryIdentifiers", [])
                if ident.get("type", "").startswith("ISBN")
            }
            if not isbns:
                continue

            data: dict[str, object] = {}
            if info.get("title"):
                data["title"] = info["title"]
            if info.get("subtitle"):
                data["subtitle"] = info["subtitle"]
            authors = info.get("authors") or []
            if authors:
                data["author"] = authors[0]
                if len(authors) > 1:
                    data["contributors"] = authors[1:]
            if info.get("publisher"):
                data["publisher"] = info["publisher"]
            if info.get("publishedDate"):
                data["publication_date"] = info["publishedDate"]
            if info.get("pageCount"):
                data["page_count"] = str(info["pageCount"])
            if info.get("categories"):
                data["subjects"] = info["categories"]
            if info.get("language"):
                data["language"] = info["language"]
            if info.get("description"):
                data["notes"] = [info["description"]]
            images = info.get("imageLinks") or {}
            if images.get("thumbnail"):
                data["cover_url"] = images["thumbnail"]

            candidates.append(
                Candidate(
                    isbns=isbns,
                    data=data,
                    record_id=item.get("id", ""),
                    raw=json.dumps(item, indent=2),
                    raw_format="json",
                )
            )
        return candidates
