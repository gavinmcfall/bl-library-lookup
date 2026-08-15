"""The stored record: schema, confidence model and field-level merging."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import IntEnum


class Confidence(IntEnum):
    """How much authority a piece of evidence carries.

    Ordered so that a higher value always wins a merge. Note that
    catalogue-in-publication data sits *below* a real post-publication
    catalogue record: CIP is supplied before the book exists, so titles, page
    counts and dates in it are provisional and are frequently wrong.
    """

    REJECT = 0
    VERY_LOW = 1  # title/author match with no matching ISBN -- never stored
    LOW = 2  # retailer listing carrying the exact ISBN
    MEDIUM = 3  # commercial metadata database
    MEDIUM_HIGH = 4  # major national/union library catalogue
    HIGH = 5  # pre-publication CIP record carrying the exact ISBN
    VERY_HIGH = 6  # published national bibliography record, exact ISBN
    # You, holding the book. For copy-specific facts -- edition number, print
    # run, signature -- this is the only authority that exists anywhere.
    USER = 7

    @property
    def label(self) -> str:
        return self.name


class Status:
    """Terminal outcome for one ISBN."""

    RESOLVED = "RESOLVED"
    INVALID_ISBN = "INVALID_ISBN"
    # Valid ISBN, reachable sources, genuinely not catalogued anywhere.
    # For Black Library limited editions this is a common, legitimate result.
    NOT_IN_NATIONAL_BIBLIOGRAPHY = "NOT_IN_NATIONAL_BIBLIOGRAPHY"
    # Not catalogued under this ISBN, but sibling editions of the same
    # title/author are. That pattern is what an uncatalogued special or
    # limited edition looks like from the outside.
    LIKELY_SPECIAL_EDITION = "LIKELY_SPECIAL_EDITION"
    # Sources errored or were blocked, so absence is not proven.
    UNRESOLVED = "UNRESOLVED"


# Bibliographic fields, in CSV column order.
BIBLIOGRAPHIC_FIELDS = (
    "isbn",
    "isbn10",
    "title",
    "subtitle",
    "author",
    "contributors",
    "publisher",
    "imprint",
    "publication_place",
    "publication_date",
    "edition_statement",
    "binding",
    "page_count",
    "dimensions",
    "language",
    "series",
    "series_number",
    "subjects",
    "dewey",
    "lc_classification",
    "bnb_number",
    "oclc_number",
    "other_identifiers",
    "notes",
    "cover_url",
)

# Collectible-edition fields. Deliberately kept apart from the bibliographic
# record: these come from publisher/retailer/archive sources, never from MARC,
# and must not be presented with library-grade authority.
COLLECTIBLE_FIELDS = (
    "limited_edition_number",
    "print_run",
    "signed",
    "special_contents",
    "cover_artist",
    "original_retail_price",
)

# Sibling-edition evidence. Held strictly apart from the bibliographic block:
# this describes a *different* ISBN, and must never be read as metadata for the
# edition in hand. It is here to identify the book, not to describe it.
SIBLING_FIELDS = (
    "sibling_isbns",
    "sibling_editions",
)

PROVENANCE_FIELDS = (
    "status",
    "confidence",
    "metadata_sources",
    "source_record_ids",
    "field_provenance",
    "date_retrieved",
    "warnings",
)

CSV_COLUMNS = (
    BIBLIOGRAPHIC_FIELDS + COLLECTIBLE_FIELDS + SIBLING_FIELDS + PROVENANCE_FIELDS
)

# Fields that accumulate values from every source rather than being overwritten
# by the winning one.
MULTI_VALUE_FIELDS = frozenset(
    {
        "contributors",
        "subjects",
        "notes",
        "other_identifiers",
        "special_contents",
        "sibling_isbns",
        "sibling_editions",
    }
)


@dataclass
class SourceRecord:
    """One source's answer for one ISBN, already proven to be an exact match."""

    source: str
    confidence: Confidence
    data: dict[str, object]
    record_id: str = ""
    raw: str = ""
    raw_format: str = ""


@dataclass
class ResolvedRecord:
    """The merged, user-facing record for a single ISBN."""

    isbn: str = ""
    isbn10: str = ""
    title: str = ""
    subtitle: str = ""
    author: str = ""
    contributors: list[str] = field(default_factory=list)
    publisher: str = ""
    imprint: str = ""
    publication_place: str = ""
    publication_date: str = ""
    edition_statement: str = ""
    binding: str = ""
    page_count: str = ""
    dimensions: str = ""
    language: str = ""
    series: str = ""
    series_number: str = ""
    subjects: list[str] = field(default_factory=list)
    dewey: str = ""
    lc_classification: str = ""
    bnb_number: str = ""
    oclc_number: str = ""
    other_identifiers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    cover_url: str = ""

    limited_edition_number: str = ""
    print_run: str = ""
    signed: str = ""
    special_contents: list[str] = field(default_factory=list)
    cover_artist: str = ""
    original_retail_price: str = ""

    sibling_isbns: list[str] = field(default_factory=list)
    sibling_editions: list[str] = field(default_factory=list)

    status: str = Status.UNRESOLVED
    confidence: str = ""
    metadata_sources: list[str] = field(default_factory=list)
    source_record_ids: list[str] = field(default_factory=list)
    field_provenance: dict[str, str] = field(default_factory=dict)
    date_retrieved: str = ""
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def _is_empty(value: object) -> bool:
    return value in (None, "", [], {})


def merge(isbn13: str, records: list[SourceRecord], retrieved_at: str) -> ResolvedRecord:
    """Fold source records into one, highest confidence winning per field.

    Sources are applied strongest-first so the first non-empty value for a
    single-valued field wins and is never overwritten by a weaker source.
    Multi-valued fields accumulate across every source, de-duplicated.
    """
    out = ResolvedRecord()
    out.isbn = isbn13
    from . import isbn as isbn_utils

    out.isbn10 = isbn_utils.to_isbn10(isbn13) or ""
    out.date_retrieved = retrieved_at

    ordered = sorted(records, key=lambda r: r.confidence, reverse=True)
    known = set(BIBLIOGRAPHIC_FIELDS) | set(COLLECTIBLE_FIELDS) | set(SIBLING_FIELDS)

    for record in ordered:
        if record.source not in out.metadata_sources:
            out.metadata_sources.append(record.source)
        if record.record_id and record.record_id not in out.source_record_ids:
            out.source_record_ids.append(record.record_id)

        for key, value in record.data.items():
            if key not in known or _is_empty(value):
                continue
            # isbn/isbn10 are set from the query, never from a source.
            if key in ("isbn", "isbn10"):
                continue

            if key in MULTI_VALUE_FIELDS:
                current = getattr(out, key)
                incoming = value if isinstance(value, list) else [value]
                for item in incoming:
                    text = str(item).strip()
                    if text and text not in current:
                        current.append(text)
                        out.field_provenance.setdefault(key, record.source)
                continue

            if _is_empty(getattr(out, key)):
                setattr(out, key, str(value).strip() if not isinstance(value, str) else value.strip())
                out.field_provenance[key] = record.source

    # Your own notes identify the copy but do not resolve the ISBN against any
    # catalogue, so a user-only record is not RESOLVED.
    catalogue_hits = [r for r in ordered if r.confidence is not Confidence.USER]
    if catalogue_hits:
        out.status = Status.RESOLVED
        out.confidence = catalogue_hits[0].confidence.label
    return out
