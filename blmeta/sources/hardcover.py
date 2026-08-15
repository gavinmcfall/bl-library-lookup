"""Hardcover.app source.

Hardcover is a community book database with a GraphQL API. Verified against
the live API: unlike the library catalogues, it holds edition-level records
for several Black Library limited editions -- with `edition_information:
"Limited Edition"`, page counts and release dates the libraries never had.
Being user-contributed it ranks MEDIUM, below any national bibliography.

Authentication is a bearer token read from the environment
(HARDCOVER_TOKEN or BOOKORBIT_HARDCOVER_TOKEN). The token travels only in
the Authorization header, never in a URL or a cache key, so it cannot reach
the on-disk response cache. Without a token the source reports itself
unavailable rather than pretending the database had no answer.
"""

from __future__ import annotations

import json
import logging
import os

from .. import isbn as isbn_utils
from ..http import FetchError
from ..record import Confidence
from .base import Candidate, Source

LOG = logging.getLogger("blmeta.source.hardcover")

API_URL = "https://api.hardcover.app/v1/graphql"
TOKEN_ENV_VARS = ("HARDCOVER_TOKEN", "BOOKORBIT_HARDCOVER_TOKEN")

# One query serves both lookups; the field to filter on is interpolated from a
# fixed two-value set, never from user input.
_EDITION_QUERY = """query($i: String!) {
  editions(where: {%s: {_eq: $i}}) {
    id isbn_13 isbn_10 title subtitle pages release_date
    physical_format edition_format edition_information
    publisher { name }
    language { language }
    book { title description cached_contributors cached_image }
  }
}"""


def read_token() -> str:
    for name in TOKEN_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


class Hardcover(Source):
    name = "hardcover"
    confidence = Confidence.MEDIUM

    def __init__(self, fetcher):
        super().__init__(fetcher)
        self._token = read_token()

    def _query(self, field: str, value: str) -> list[dict]:
        body = json.dumps(
            {"query": _EDITION_QUERY % field, "variables": {"i": value}}
        )
        response = self.fetcher.post(
            API_URL, body, headers={"Authorization": f"Bearer {self._token}"}
        )
        try:
            payload = json.loads(response.body)
        except json.JSONDecodeError:
            return []
        if payload.get("errors"):
            raise FetchError(f"hardcover: {payload['errors'][0].get('message', 'error')}")
        return payload.get("data", {}).get("editions", []) or []

    def search(self, isbn13: str) -> list[Candidate]:
        if not self._token:
            raise FetchError(
                "hardcover: no token in HARDCOVER_TOKEN / BOOKORBIT_HARDCOVER_TOKEN"
            )

        editions = self._query("isbn_13", isbn13)
        if not editions:
            isbn10 = isbn_utils.to_isbn10(isbn13)
            if isbn10:
                editions = self._query("isbn_10", isbn10)

        candidates: list[Candidate] = []
        for edition in editions:
            # Verified live: Hardcover's isbn_10 field sometimes carries a
            # *different edition's* ISBN, so every asserted ISBN goes into the
            # candidate and the base class insists on an exact match as usual.
            isbns = {
                value
                for value in (edition.get("isbn_13"), edition.get("isbn_10"))
                if value
            }
            if not isbns:
                continue
            candidates.append(
                Candidate(
                    isbns=isbns,
                    data=self._map(edition),
                    record_id=str(edition.get("id", "")),
                    raw=json.dumps(edition, indent=2),
                    raw_format="json",
                )
            )
        return candidates

    @staticmethod
    def _map(edition: dict) -> dict[str, object]:
        data: dict[str, object] = {}
        book = edition.get("book") or {}

        if edition.get("title"):
            data["title"] = edition["title"]
        if edition.get("subtitle"):
            data["subtitle"] = edition["subtitle"]
        if edition.get("pages"):
            data["page_count"] = str(edition["pages"])
        if edition.get("release_date"):
            data["publication_date"] = edition["release_date"]

        binding = edition.get("physical_format") or edition.get("edition_format")
        if binding:
            data["binding"] = str(binding).lower()

        # e.g. "Limited Edition" -- edition-level detail the libraries lack.
        if edition.get("edition_information"):
            data["edition_statement"] = edition["edition_information"]

        publisher = (edition.get("publisher") or {}).get("name")
        if publisher:
            data["publisher"] = publisher
        language = (edition.get("language") or {}).get("language")
        if language:
            data["language"] = language

        authors = [
            (entry.get("author") or {}).get("name", "")
            for entry in (book.get("cached_contributors") or [])
        ]
        authors = [name for name in authors if name]
        if authors:
            data["author"] = authors[0]
            if len(authors) > 1:
                data["contributors"] = authors[1:]

        image = book.get("cached_image") or {}
        if image.get("url"):
            data["cover_url"] = image["url"]
        if book.get("description"):
            data["notes"] = [book["description"]]

        return data
