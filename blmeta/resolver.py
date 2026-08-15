"""The resolution pipeline for a single ISBN."""

from __future__ import annotations

import datetime as dt
import logging
import re
from pathlib import Path

from . import isbn as isbn_utils
from .http import FetchError, Fetcher
from .record import Confidence, ResolvedRecord, SourceRecord, Status, merge
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

# Fields that belong to the *work* and are therefore genuinely shared between
# editions, so they may be inherited from a sibling under --inherit-siblings.
# Everything else (page count, dimensions, binding, edition statement,
# publication date, price, cover) differs between editions by definition and is
# never inherited.
INHERITABLE_FIELDS = (
    "author",
    "contributors",
    "publisher",
    "imprint",
    "publication_place",
    "series",
    "series_number",
    "subjects",
    "dewey",
    "lc_classification",
    "language",
)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalise_title(value: str) -> str:
    value = re.sub(r"\s*[:/].*$", "", value)  # drop subtitle and responsibility
    value = re.sub(r"^(the|a|an)\s+", "", value.strip(), flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _same_work(query: str, candidate: str) -> bool:
    """Whether a catalogue hit is really the same work as the one asked for.

    Catalogue title indexes match loosely -- searching "Dante" also returns
    "Accounting for Dante". Requiring the normalised titles to be equal keeps
    sibling identification honest.
    """
    left, right = _normalise_title(query), _normalise_title(candidate)
    return bool(left) and left == right


class Resolver:
    def __init__(
        self,
        fetcher: Fetcher,
        source_names: tuple[str, ...] = DEFAULT_SOURCES,
        raw_dir: Path | None = None,
        inherit_siblings: bool = False,
        publisher_hint: str = "Black Library",
    ):
        self.fetcher = fetcher
        self.raw_dir = raw_dir
        self.inherit_siblings = inherit_siblings
        self.publisher_hint = publisher_hint
        self.sources = []
        for name in source_names:
            cls = REGISTRY.get(name)
            if not cls:
                raise ValueError(f"unknown source: {name}")
            self.sources.append(cls(fetcher))

    def resolve(
        self, raw_isbn: str, user_data: dict[str, object] | None = None
    ) -> ResolvedRecord:
        user_data = {k: v for k, v in (user_data or {}).items() if v not in (None, "", [])}
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
        if user_data:
            found.append(
                SourceRecord(source="user", confidence=Confidence.USER, data=dict(user_data))
            )
        warnings: list[str] = []
        national_answered = False
        national_silent = True
        secondary_failed = False

        catalogue_hit = False
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
                catalogue_hit = True
                if source.is_national_bibliography:
                    national_silent = False
                if self.raw_dir:
                    self._save_raw(isbn13, result)

        # The catalogue does not hold this ISBN. If we know what the book is,
        # look for its other editions -- that is what identifies an
        # uncatalogued limited edition.
        sibling_note = None
        if not catalogue_hit:
            sibling_note = self._find_siblings(isbn13, user_data, found, warnings)

        record = merge(isbn13, found, _utc_now())
        record.warnings.extend(warnings)

        if sibling_note:
            record.status = Status.LIKELY_SPECIAL_EDITION
            record.warnings.append(sibling_note)
            self._flag_special_edition(record)
            return record

        if not catalogue_hit:
            # Distinguish proven absence from failure to look. Only the
            # national bibliography source's silence settles this; a blocked
            # retailer or union catalogue does not.
            if national_answered:
                record.status = Status.NOT_IN_NATIONAL_BIBLIOGRAPHY
                record.warnings.append(
                    "No exact-ISBN record in any queried source. For Black Library "
                    "limited editions this is common: special editions frequently "
                    "never receive their own catalogue record. Supply a title (and "
                    "author) for this ISBN to search for its sibling editions, "
                    "which is how an uncatalogued limited edition is identified."
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

    def _find_siblings(
        self,
        isbn13: str,
        user_data: dict[str, object],
        found: list[SourceRecord],
        warnings: list[str],
    ) -> str | None:
        """Identify an uncatalogued edition by finding its siblings.

        Returns a human-readable note when sibling editions were found, or
        None. The sibling metadata is recorded in dedicated ``sibling_*``
        fields only -- it describes other ISBNs, so letting it reach the
        bibliographic fields would be exactly the substitution this tool
        exists to prevent.
        """
        title = str(user_data.get("title") or "").strip()
        author = str(user_data.get("author") or "").strip()
        if not title:
            return None

        # Only the surname is needed, and MARC stores names inverted
        # ("Haley, Guy"), so a full "Guy Haley" string matches poorly.
        surname = author.split(",")[0].split()[-1] if author else ""
        publisher = str(user_data.get("publisher") or self.publisher_hint or "").strip()

        # A title alone is too weak a query to find siblings, so pair it with a
        # second term. Each strategy is tried until one lands; a wrong hint
        # simply returns nothing and falls through to the next.
        strategies: list[tuple[str, str]] = []
        if surname:
            strategies.append((surname, ""))
        if publisher:
            strategies.append(("", publisher))
        strategies.append(("", ""))

        siblings: list[tuple[str, str]] = []
        inheritable: list[SourceRecord] = []
        for source in self.sources:
            searcher = getattr(source, "search_siblings", None)
            if not searcher:
                continue

            candidates: list = []
            for strategy_author, strategy_publisher in strategies:
                try:
                    found_now = searcher(title, strategy_author, strategy_publisher)
                except FetchError as exc:
                    warnings.append(f"{source.name}: sibling search unavailable ({exc})")
                    break
                except Exception as exc:
                    LOG.warning("%s sibling search failed: %s", source.name, exc)
                    break
                if any(_same_work(title, str(c.data.get("title", ""))) for c in found_now):
                    candidates = found_now
                    break

            for candidate in candidates:
                data = candidate.data
                # Guard against loose catalogue matching: require the title to
                # actually correspond, not merely to contain the search term.
                if not _same_work(title, str(data.get("title", ""))):
                    continue
                for other in sorted(candidate.isbns):
                    if isbn_utils.matches(isbn13, other):
                        continue
                    label = " ".join(
                        part
                        for part in (
                            str(data.get("publication_date", "")),
                            str(data.get("edition_statement", "")) or None,
                            str(data.get("binding", "")) or None,
                        )
                        if part
                    ).strip()
                    siblings.append((other, f"{other} ({label})" if label else other))

                if self.inherit_siblings:
                    shared = {
                        key: value
                        for key, value in data.items()
                        if key in INHERITABLE_FIELDS and value
                    }
                    if shared:
                        first = sorted(candidate.isbns)[0]
                        inheritable.append(
                            SourceRecord(
                                # Provenance names the ISBN the value came
                                # from, so an inherited field is never mistaken
                                # for one observed on this edition.
                                source=f"sibling:{first}",
                                confidence=Confidence.MEDIUM,
                                data=shared,
                            )
                        )

        if not siblings:
            return None

        seen: set[str] = set()
        isbns: list[str] = []
        descriptions: list[str] = []
        for other, description in siblings:
            if other in seen:
                continue
            seen.add(other)
            isbns.append(other)
            descriptions.append(description)

        found.append(
            SourceRecord(
                source="sibling-search",
                confidence=Confidence.USER,  # ranked high so it is never overwritten
                data={"sibling_isbns": isbns, "sibling_editions": descriptions},
            )
        )
        found.extend(inheritable)
        if inheritable:
            warnings.append(
                "Work-level fields (author, publisher, series, classification) "
                "were inherited from a sibling edition; field_provenance names "
                "the ISBN each came from. Edition-specific fields were not "
                "inherited."
            )
        return (
            f"Not catalogued under this ISBN, but {len(isbns)} sibling edition(s) of "
            f"'{title}' are: {', '.join(isbns)}. A valid publisher ISBN with no "
            "catalogue record of its own, alongside catalogued siblings, is the "
            "signature of an uncatalogued special/limited edition. Sibling data is "
            "kept in the sibling_* columns and is NOT metadata for this edition."
        )

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
