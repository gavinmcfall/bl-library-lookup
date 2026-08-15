"""The resolution pipeline for a single ISBN."""

from __future__ import annotations

import datetime as dt
import logging
import re
from pathlib import Path

from . import isbn as isbn_utils
from .http import FetchError, Fetcher
from .record import ResolvedRecord, SourceRecord, Status, merge
from .sources import DEFAULT_SOURCES, REGISTRY

LOG = logging.getLogger("blmeta.resolver")

# Signals that a record describes a collectible edition. Drawn only from the
# bibliographic record itself (edition statement, notes, ISBN qualifiers), never
# guessed from the title.
_SPECIAL_EDITION = re.compile(
    r"\b(limited|special|collector'?s|deluxe|anniversary|exclusive|signed|"
    r"slipcase[d]?|numbered)\b",
    re.IGNORECASE,
)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Resolver:
    def __init__(
        self,
        fetcher: Fetcher,
        source_names: tuple[str, ...] = DEFAULT_SOURCES,
        raw_dir: Path | None = None,
    ):
        self.fetcher = fetcher
        self.raw_dir = raw_dir
        self.sources = []
        for name in source_names:
            cls = REGISTRY.get(name)
            if not cls:
                raise ValueError(f"unknown source: {name}")
            self.sources.append(cls(fetcher))

    def resolve(self, raw_isbn: str) -> ResolvedRecord:
        isbn13 = isbn_utils.canonical(raw_isbn)
        if not isbn13:
            record = ResolvedRecord(
                isbn=isbn_utils.clean(raw_isbn),
                status=Status.INVALID_ISBN,
                date_retrieved=_utc_now(),
            )
            record.warnings.append(
                f"'{raw_isbn.strip()}' is not a valid ISBN (checksum or length failed)"
            )
            return record

        found: list[SourceRecord] = []
        warnings: list[str] = []
        national_answered = False
        national_silent = True
        secondary_failed = False

        for source in self.sources:
            try:
                result = source.lookup(isbn13)
            except FetchError as exc:
                warnings.append(f"{source.name}: unavailable ({exc})")
                if not source.is_national_bibliography:
                    secondary_failed = True
                continue
            except Exception as exc:  # a malformed response must not kill the run
                LOG.warning("%s failed for %s: %s", source.name, isbn13, exc)
                warnings.append(f"{source.name}: error ({type(exc).__name__})")
                if not source.is_national_bibliography:
                    secondary_failed = True
                continue

            # The source answered without error, so its silence is evidence.
            if source.is_national_bibliography:
                national_answered = True

            if result:
                found.append(result)
                if source.is_national_bibliography:
                    national_silent = False
                if self.raw_dir:
                    self._save_raw(isbn13, result)

        record = merge(isbn13, found, _utc_now())
        record.warnings.extend(warnings)

        if not found:
            # Distinguish proven absence from failure to look. Only the
            # national bibliography source's silence settles this; a blocked
            # retailer or union catalogue does not.
            if national_answered:
                record.status = Status.NOT_IN_NATIONAL_BIBLIOGRAPHY
                record.warnings.append(
                    "No exact-ISBN record in any queried source. For Black Library "
                    "limited editions this is common: special editions frequently "
                    "never receive their own catalogue record. Check the trade "
                    "edition's ISBN for shared bibliographic data, and the "
                    "publisher/archive record for edition-specific details."
                )
                if secondary_failed:
                    record.warnings.append(
                        "Some secondary sources were unreachable, so enrichment "
                        "may be incomplete."
                    )
            else:
                record.status = Status.UNRESOLVED
                record.warnings.append(
                    "No exact-ISBN match found, and the national bibliography "
                    "source could not be reached, so absence is not proven."
                )
        elif national_answered and national_silent:
            record.warnings.append(
                "Not held by the national bibliography source; metadata comes "
                "from secondary catalogues only."
            )

        self._flag_special_edition(record)
        return record

    @staticmethod
    def _flag_special_edition(record: ResolvedRecord) -> None:
        """Record evidence that this is a collectible edition.

        Evidence-based only: we look at the edition statement, notes and any
        binding qualifier the cataloguer supplied.
        """
        haystack = " ".join(
            [record.edition_statement, record.binding, " ".join(record.notes)]
        )
        match = _SPECIAL_EDITION.search(haystack)
        if match and not record.special_contents:
            record.special_contents.append(
                f"Catalogue evidence of special edition: '{match.group(0)}'"
            )
        if re.search(r"\bsigned\b", haystack, re.IGNORECASE):
            record.signed = "yes (per catalogue note)"

    def _save_raw(self, isbn13: str, result: SourceRecord) -> None:
        if not result.raw:
            return
        directory = self.raw_dir / isbn13
        directory.mkdir(parents=True, exist_ok=True)
        suffix = "xml" if result.raw_format == "marcxml" else result.raw_format or "txt"
        (directory / f"{result.source}.{suffix}").write_text(result.raw, encoding="utf-8")
