"""MARCXML -> flat metadata dictionary.

Only the fields we actually store are extracted; everything else stays in the
preserved raw record so nothing is lost.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from . import isbn as isbn_utils

MARC_NS = "http://www.loc.gov/MARC21/slim"
SRW_NS = "http://www.loc.gov/zing/srw/"

_PAGES = re.compile(r"(\d[\d,]*)\s*(?:p\b|pages|leaves)", re.IGNORECASE)
_DATE = re.compile(r"(1[5-9]\d{2}|20\d{2})")

# MARC language codes we are likely to meet on Black Library material.
_LANGUAGES = {"eng": "English", "fre": "French", "ger": "German", "spa": "Spanish", "ita": "Italian"}


def _text(node: ET.Element | None) -> str:
    return (node.text or "").strip() if node is not None else ""


def _subfields(field: ET.Element) -> list[tuple[str, str]]:
    return [
        (sub.get("code", ""), (sub.text or "").strip())
        for sub in field.findall(f"{{{MARC_NS}}}subfield")
    ]


def _joined(field: ET.Element, codes: str, separator: str = " ") -> str:
    parts = [value for code, value in _subfields(field) if code in codes and value]
    return separator.join(parts).strip()


def _first(field: ET.Element, code: str) -> str:
    for sub_code, value in _subfields(field):
        if sub_code == code:
            return value
    return ""


def _tidy(value: str) -> str:
    """Strip ISBD punctuation that MARC uses to glue fields together."""
    return value.strip().rstrip(" /:;,=").strip()


def iter_records(xml_text: str) -> list[ET.Element]:
    """Return every MARC record element in an SRU response."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    records = root.findall(f".//{{{MARC_NS}}}record")
    if not records and root.tag == f"{{{MARC_NS}}}record":
        records = [root]
    return records


def record_isbns(record: ET.Element) -> set[str]:
    """Every ISBN asserted by a MARC record, canonicalised to ISBN-13."""
    found: set[str] = set()
    for field in record.findall(f"{{{MARC_NS}}}datafield"):
        if field.get("tag") not in ("020", "024"):
            continue
        for code, value in _subfields(field):
            if code not in ("a", "z"):
                continue
            # $z is a cancelled/invalid ISBN -- deliberately excluded, it must
            # never be used to claim an edition match.
            if code == "z":
                continue
            parsed = isbn_utils.extract(value)
            if parsed:
                found.add(parsed)
    return found


def parse(record: ET.Element) -> dict[str, object]:
    """Extract stored fields from one MARCXML record."""
    out: dict[str, object] = {}
    contributors: list[str] = []
    subjects: list[str] = []
    notes: list[str] = []
    identifiers: list[str] = []
    series: list[str] = []

    control = {
        node.get("tag"): (node.text or "")
        for node in record.findall(f"{{{MARC_NS}}}controlfield")
    }
    field_008 = control.get("008", "")
    if len(field_008) >= 38:
        code = field_008[35:38].strip()
        if code:
            out["language"] = _LANGUAGES.get(code, code)

    for field in record.findall(f"{{{MARC_NS}}}datafield"):
        tag = field.get("tag", "")

        if tag == "020":
            value = _first(field, "a")
            hint = isbn_utils.binding_hint(value)
            if hint and "binding" not in out:
                out["binding"] = hint
            price = _first(field, "c")
            if price and "original_retail_price" not in out:
                out["original_retail_price"] = _tidy(price)

        elif tag == "100":
            name = _tidy(_first(field, "a"))
            if name:
                out.setdefault("author", name)

        elif tag == "110":
            name = _tidy(_first(field, "a"))
            if name:
                out.setdefault("author", name)

        elif tag == "700":
            name = _tidy(_first(field, "a"))
            role = _first(field, "e")
            if name:
                contributors.append(f"{name} ({_tidy(role)})" if role else name)
                if "illustrator" in role.lower() or "artist" in role.lower():
                    out.setdefault("cover_artist", name)

        elif tag == "245":
            title = _tidy(_first(field, "a"))
            subtitle = _tidy(_first(field, "b"))
            if title:
                out.setdefault("title", title)
            if subtitle:
                out.setdefault("subtitle", subtitle)

        elif tag == "250":
            statement = _tidy(_joined(field, "ab"))
            if statement:
                out.setdefault("edition_statement", statement)

        elif tag in ("260", "264"):
            # 264 with indicator2 == 4 is a copyright date, not publication.
            if tag == "264" and field.get("ind2") == "4":
                continue
            place = _tidy(_first(field, "a"))
            publisher = _tidy(_first(field, "b"))
            date = _first(field, "c")
            if place:
                out.setdefault("publication_place", place)
            if publisher:
                out.setdefault("publisher", publisher)
            if date:
                match = _DATE.search(date)
                out.setdefault("publication_date", match.group(1) if match else _tidy(date))

        elif tag == "300":
            extent = _first(field, "a")
            dimensions = _tidy(_first(field, "c"))
            if extent:
                match = _PAGES.search(extent)
                if match:
                    out.setdefault("page_count", match.group(1).replace(",", ""))
                else:
                    notes.append(f"Extent: {_tidy(extent)}")
            if dimensions:
                out.setdefault("dimensions", dimensions)

        elif tag in ("490", "830", "440"):
            name = _tidy(_first(field, "a"))
            number = _tidy(_first(field, "v"))
            if name:
                series.append(name)
            if number:
                out.setdefault("series_number", number)

        elif tag == "505":
            # Formatted contents: the works collected in an omnibus.
            contents = _joined(field, "atr", " ")
            if contents:
                out["contents"] = contents

        elif tag in ("500", "501", "502", "504", "520", "521", "586"):
            note = _tidy(_joined(field, "a"))
            if note:
                notes.append(note)

        elif tag in ("650", "651", "655"):
            subject = " -- ".join(
                value for code, value in _subfields(field) if code in "axvyz" and value
            )
            if subject:
                subjects.append(_tidy(subject))

        elif tag == "082":
            dewey = _first(field, "a")
            if dewey:
                out.setdefault("dewey", _tidy(dewey))

        elif tag == "050":
            lc = _joined(field, "ab")
            if lc:
                out.setdefault("lc_classification", _tidy(lc))

        elif tag == "035":
            value = _first(field, "a")
            if not value:
                continue
            identifiers.append(value)
            lowered = value.lower()
            if "ocolc" in lowered or "worldcat" in lowered:
                digits = re.sub(r"\D", "", value)
                if digits:
                    out.setdefault("oclc_number", digits)
            elif value.startswith("(Uk)"):
                # British Library / BNB-derived control number.
                out.setdefault("bnb_number", value[4:])

    if series:
        # A title often carries both 490 and 830 forms of the same series
        # ("Blood angels" / "Blood Angels."). Keep every distinct series, but
        # collapse forms differing only in case or trailing punctuation.
        unique: dict[str, str] = {}
        for name in series:
            key = re.sub(r"[^a-z0-9]", "", name.lower())
            if key and key not in unique:
                unique[key] = name
        out["series"] = " ; ".join(unique.values())
    if contributors:
        out["contributors"] = contributors
    if subjects:
        out["subjects"] = subjects
    if notes:
        out["notes"] = notes
    if identifiers:
        out["other_identifiers"] = identifiers

    return out


def record_id(record: ET.Element) -> str:
    """The source system's own record identifier (MARC 001)."""
    for node in record.findall(f"{{{MARC_NS}}}controlfield"):
        if node.get("tag") == "001":
            return (node.text or "").strip()
    return ""


def to_xml(record: ET.Element) -> str:
    return ET.tostring(record, encoding="unicode")
