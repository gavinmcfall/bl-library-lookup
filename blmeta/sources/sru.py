"""SRU (Search/Retrieve via URL) catalogue sources.

SRU replaces the Z39.50 approach: it is the same underlying catalogue index,
but over plain HTTPS. That removes the yaz-client dependency and works from
anywhere that can reach the web -- raw Z39.50 on port 1921 is blocked by most
corporate networks and proxies.
"""

from __future__ import annotations

import logging
import urllib.parse

from .. import marc
from ..http import FetchError
from ..record import Confidence
from .base import Candidate, Source

LOG = logging.getLogger("blmeta.source.sru")


class SRUSource(Source):
    """Generic SRU/MARCXML catalogue."""

    base_url = ""
    isbn_index = "isbn"
    sru_version = "1.2"
    record_schema = "marcxml"

    title_index = "alma.title"
    creator_index = "alma.creator"
    publisher_index = "alma.publisher"

    def _url(self, query: str, limit: int = 10) -> str:
        params = {
            "version": self.sru_version,
            "operation": "searchRetrieve",
            "recordSchema": self.record_schema,
            "maximumRecords": str(limit),
            "query": query,
        }
        return f"{self.base_url}?{urllib.parse.urlencode(params)}"

    def _candidates_from(self, url: str) -> list[Candidate]:
        response = self.fetcher.get(url, accept="application/xml")
        candidates: list[Candidate] = []
        for record in marc.iter_records(response.body):
            isbns = marc.record_isbns(record)
            if not isbns:
                continue
            candidates.append(
                Candidate(
                    isbns=isbns,
                    data=marc.parse(record),
                    record_id=marc.record_id(record),
                    raw=marc.to_xml(record),
                    raw_format="marcxml",
                )
            )
        return candidates

    def search_siblings(
        self, title: str, author: str = "", publisher: str = ""
    ) -> list[Candidate]:
        """Find other editions of the same work.

        Used only to *identify* an ISBN the catalogue does not hold. The
        results describe different editions and are never merged into the
        record's own bibliographic fields.

        A title alone is rarely enough: "Dante" matches over 1700 records and
        the real book is not in the first page of results. A second term --
        author or publisher -- is what makes the search land.
        """
        if not title:
            return []
        # Quoting each term matters: unquoted terms are ORed by Alma and return
        # thousands of unrelated hits.
        clauses = [f'{self.title_index}="{title}"']
        if author:
            clauses.append(f'{self.creator_index}="{author}"')
        if publisher:
            clauses.append(f'{self.publisher_index}="{publisher}"')
        return self._candidates_from(self._url(" and ".join(clauses), limit=25))

    def search(self, isbn13: str) -> list[Candidate]:
        try:
            return self._candidates_from(self._url(f"{self.isbn_index}={isbn13}"))
        except FetchError as exc:
            LOG.info("%s unavailable: %s", self.name, exc)
            raise


class NationalLibraryOfScotland(SRUSource):
    """NLS holds British National Bibliography-derived records.

    Verified: matched records carry MARC 035 ``(Uk)...`` control numbers,
    confirming BL/BNB provenance. Note that NLS is *not* the British Library --
    it receives legal deposit material via the Agency for Legal Deposit
    Libraries, so absence here does not prove absence from the BL.
    """

    name = "nls"
    confidence = Confidence.VERY_HIGH
    is_national_bibliography = True
    base_url = "https://nls.alma.exlibrisgroup.com/view/sru/44NLS_INST"
    isbn_index = "alma.isbn"


class LibraryHubDiscover(SRUSource):
    """Jisc Library Hub Discover -- UK research library union catalogue.

    Frequently behind a bot challenge; treated as best-effort.
    """

    name = "libraryhub"
    confidence = Confidence.MEDIUM_HIGH
    is_national_bibliography = False
    base_url = "https://discover.libraryhub.jisc.ac.uk/sru-api"
    sru_version = "1.1"
    isbn_index = "bath.isbn"
