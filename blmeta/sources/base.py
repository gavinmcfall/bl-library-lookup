"""Source interface.

Every source returns candidates; the base class -- not the source -- decides
whether a candidate is an exact ISBN match. This is the single chokepoint that
enforces the core principle, so no adapter can accidentally relax it.
"""

from __future__ import annotations

import logging

from .. import isbn as isbn_utils
from ..http import Fetcher
from ..record import Confidence, SourceRecord

LOG = logging.getLogger("blmeta.source")


class Candidate:
    """A possible record, before exact-ISBN verification."""

    def __init__(
        self,
        isbns: set[str],
        data: dict[str, object],
        record_id: str = "",
        raw: str = "",
        raw_format: str = "",
    ):
        self.isbns = isbns
        self.data = data
        self.record_id = record_id
        self.raw = raw
        self.raw_format = raw_format


class Source:
    name = "source"
    confidence = Confidence.MEDIUM
    # Set on sources whose silence is meaningful evidence of absence from the
    # national bibliography (as opposed to merely "this shop doesn't stock it").
    is_national_bibliography = False

    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher

    def search(self, isbn13: str) -> list[Candidate]:
        raise NotImplementedError

    def lookup(self, isbn13: str) -> SourceRecord | None:
        """Search, then admit a candidate only on an exact ISBN match."""
        candidates = self.search(isbn13)
        for candidate in candidates:
            if any(isbn_utils.matches(isbn13, found) for found in candidate.isbns):
                return SourceRecord(
                    source=self.name,
                    confidence=self.confidence,
                    data=candidate.data,
                    record_id=candidate.record_id,
                    raw=candidate.raw,
                    raw_format=candidate.raw_format,
                )
        if candidates:
            # The source held records for this query but none carried our ISBN.
            # That is a near miss on another edition -- explicitly rejected.
            LOG.debug(
                "%s: %d candidate(s) for %s, none an exact ISBN match",
                self.name,
                len(candidates),
                isbn13,
            )
        return None
