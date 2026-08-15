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

# Of those, these describe the *issue* rather than the work. A sibling record
# may be a reissue by a different house -- a Hachette partwork of a Black
# Library novel, say -- and carrying its imprint across would misdescribe the
# book in hand. They are inherited only from a sibling published by the same
# house, judged against the publisher hint.
ISSUE_LEVEL_FIELDS = frozenset({"publisher", "imprint", "publication_place"})


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalise_title(value: str) -> str:
    value = re.sub(r"\s*[:/].*$", "", value)  # drop subtitle and responsibility
    value = re.sub(r"^(the|a|an)\s+", "", value.strip(), flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _collects_work(query: str, candidate: dict[str, object]) -> bool:
    """Whether a volume collects the queried work.

    An omnibus lists its contents in the 245 subtitle or a 505 note, as
    colon- or dash-separated titles ("flesh of Cretacia : Sons of Wrath :
    Trial by blood"). Each segment is compared whole, so a novella is matched
    only by its own title and never by a stray substring.
    """
    target = _normalise_title(query)
    if not target:
        return False
    haystack = " : ".join(
        str(candidate.get(key, "")) for key in ("subtitle", "contents") if candidate.get(key)
    )
    for segment in re.split(r"[:;/]|--", haystack):
        if _normalise_title(segment) == target:
            return True
    return False


def _tidy_label(data: dict[str, object]) -> str:
    """Short human description of a catalogue record: title, year."""
    parts = [str(data.get("title", "")), str(data.get("publication_date", ""))]
    return " ".join(p for p in parts if p).strip()


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

        # Sibling ISBNs you supply are verified, never taken on trust: each is
        # looked up by exact ISBN like any other query.
        declared = user_data.pop("declared_siblings", [])
        if isinstance(declared, str):
            declared = [declared]

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
            if declared:
                sibling_note = self._verify_declared(isbn13, declared, found, warnings)
            if not sibling_note:
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

    def _verify_declared(
        self,
        isbn13: str,
        declared: list[str],
        found: list[SourceRecord],
        warnings: list[str],
    ) -> str | None:
        """Check sibling ISBNs you supplied, and record what they turn out to be.

        Knowing the trade edition's ISBN is common -- it is often printed on a
        listing or the book itself -- and it identifies an uncatalogued edition
        far more directly than a title search. Each one is still looked up by
        exact ISBN, so a mistyped or wrong number is reported, not believed.
        """
        confirmed: list[str] = []
        descriptions: list[str] = []

        for raw in declared:
            other = isbn_utils.canonical(raw)
            if not other:
                warnings.append(f"declared sibling '{raw}' is not a valid ISBN")
                continue
            if isbn_utils.matches(isbn13, other):
                warnings.append(
                    f"declared sibling {other} is this edition's own ISBN; ignored"
                )
                continue

            record = None
            for source in self.sources:
                try:
                    record = source.lookup(other)
                except Exception:  # a failed sibling check is not fatal
                    continue
                if record:
                    break

            if not record:
                warnings.append(
                    f"declared sibling {other} could not be confirmed in any source; "
                    "recorded as unverified"
                )
                confirmed.append(other)
                descriptions.append(f"{other} (unverified)")
                continue

            data = record.data
            label = _tidy_label(data) or "confirmed"
            confirmed.append(other)
            descriptions.append(f"{other} ({label})")

            if self.inherit_siblings:
                publisher = self.publisher_hint
                sibling_publisher = str(data.get("publisher", ""))
                same_house = not publisher or not sibling_publisher or (
                    publisher.lower() in sibling_publisher.lower()
                )
                shared = {
                    key: value
                    for key, value in data.items()
                    if key in INHERITABLE_FIELDS
                    and value
                    and (same_house or key not in ISSUE_LEVEL_FIELDS)
                }
                if shared:
                    found.append(
                        SourceRecord(
                            source=f"sibling:{other}",
                            confidence=Confidence.MEDIUM,
                            data=shared,
                        )
                    )

        if not confirmed:
            return None

        found.append(
            SourceRecord(
                source="declared-sibling",
                confidence=Confidence.USER,
                data={"sibling_isbns": confirmed, "sibling_editions": descriptions},
            )
        )
        return (
            f"Not catalogued under this ISBN. You supplied {len(confirmed)} sibling "
            f"edition(s): {', '.join(descriptions)}. An uncatalogued ISBN alongside a "
            "known sibling edition is the signature of a special/limited edition. "
            "Sibling data stays in the sibling_* columns."
        )

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
        collections: list[str] = []
        inheritable: list[SourceRecord] = []
        evidence_found = False
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
                # Accept a result set that either holds another edition of the
                # work or a volume collecting it; both are useful answers.
                if any(
                    _same_work(title, str(c.data.get("title", "")))
                    or _collects_work(title, c.data)
                    for c in found_now
                ):
                    candidates = found_now
                    break

            for candidate in candidates:
                data = candidate.data
                # Guard against loose catalogue matching: require the title to
                # actually correspond, not merely to contain the search term.
                if not _same_work(title, str(data.get("title", ""))):
                    # An omnibus that collects this work is not another edition
                    # of it, so it never counts as a sibling. It is still worth
                    # recording: it proves the text was published and says where
                    # it can be found in print.
                    if _collects_work(title, data):
                        for other in sorted(candidate.isbns):
                            if isbn_utils.matches(isbn13, other):
                                continue
                            collections.append(
                                f"{other} ({_tidy_label(data)})"
                                if _tidy_label(data)
                                else other
                            )
                    continue

                evidence_found = True
                label = " ".join(
                    part
                    for part in (
                        str(data.get("publication_date", "")),
                        str(data.get("edition_statement", "")) or None,
                        str(data.get("binding", "")) or None,
                    )
                    if part
                ).strip()

                others = [
                    other
                    for other in sorted(candidate.isbns)
                    if not isbn_utils.matches(isbn13, other)
                ]
                if others:
                    for other in others:
                        siblings.append((other, f"{other} ({label})" if label else other))
                else:
                    # A catalogued record carrying no ISBN of its own. It
                    # cannot supply a sibling ISBN, but it does establish that
                    # the work is catalogued while this edition is not.
                    publisher_seen = str(data.get("publisher", "")) or "unknown publisher"
                    descriptor = f"(no ISBN in record) {publisher_seen}"
                    if label:
                        descriptor += f", {label}"
                    siblings.append(("", descriptor))

                if self.inherit_siblings:
                    sibling_publisher = str(data.get("publisher", ""))
                    same_house = not publisher or not sibling_publisher or (
                        publisher.lower() in sibling_publisher.lower()
                    )
                    shared = {
                        key: value
                        for key, value in data.items()
                        if key in INHERITABLE_FIELDS
                        and value
                        and (same_house or key not in ISSUE_LEVEL_FIELDS)
                    }
                    if not same_house:
                        warnings.append(
                            f"Sibling record is published by '{sibling_publisher}', "
                            f"not '{publisher}'; its imprint details were not "
                            "inherited."
                        )
                    if shared:
                        known = sorted(candidate.isbns)
                        origin = known[0] if known else (candidate.record_id or "no-isbn")
                        inheritable.append(
                            SourceRecord(
                                # Provenance names the ISBN the value came
                                # from, so an inherited field is never mistaken
                                # for one observed on this edition.
                                source=f"sibling:{origin}",
                                confidence=Confidence.MEDIUM,
                                data=shared,
                            )
                        )

        if not evidence_found:
            if collections:
                unique_collections = list(dict.fromkeys(collections))
                found.append(
                    SourceRecord(
                        source="collection-search",
                        confidence=Confidence.USER,
                        data={"collected_in": unique_collections},
                    )
                )
                return (
                    f"Not catalogued under this ISBN, and no separate edition of "
                    f"'{title}' is catalogued either. The text is in print only as "
                    f"part of {', '.join(unique_collections)}. A collection is a "
                    "different work, so it is recorded in collected_in and lends "
                    "this record nothing."
                )
            return None

        seen: set[str] = set()
        isbns: list[str] = []
        descriptions: list[str] = []
        for other, description in siblings:
            if description in seen:
                continue
            seen.add(description)
            if other:
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
        if isbns:
            detail = f"{len(isbns)} sibling edition(s) are: {', '.join(isbns)}"
        else:
            detail = (
                "the work is catalogued, but the matching record carries no ISBN "
                "of its own, so no sibling ISBN can be cited"
            )
        return (
            f"Not catalogued under this ISBN. For '{title}', {detail}. A valid "
            "publisher ISBN with no catalogue record of its own, alongside a "
            "catalogued record of the same work, is the signature of an "
            "uncatalogued special/limited edition. Sibling data is kept in the "
            "sibling_* columns and is NOT metadata for this edition."
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
