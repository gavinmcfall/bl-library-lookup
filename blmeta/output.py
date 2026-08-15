"""CSV and JSON serialisation."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable, TextIO

from .record import CSV_COLUMNS, ResolvedRecord

LIST_SEPARATOR = " | "


def _flatten(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return LIST_SEPARATOR.join(str(item) for item in value if str(item).strip())
    if isinstance(value, dict):
        return LIST_SEPARATOR.join(f"{k}={v}" for k, v in sorted(value.items()))
    return str(value)


def to_row(record: ResolvedRecord) -> dict[str, str]:
    data = record.as_dict()
    return {column: _flatten(data.get(column)) for column in CSV_COLUMNS}


def write_csv(records: Iterable[ResolvedRecord], handle: TextIO) -> int:
    writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS), lineterminator="\n")
    writer.writeheader()
    count = 0
    for record in records:
        writer.writerow(to_row(record))
        count += 1
    return count


def write_json(records: Iterable[ResolvedRecord], path: Path) -> None:
    payload = [record.as_dict() for record in records]
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


# Fields no online source will ever hold for a collectible edition: they are
# facts about the physical object, readable only from the book in hand.
FROM_THE_BOOK = (
    "limited_edition_number",
    "print_run",
    "signed",
    "cover_artist",
    "original_retail_price",
    "publication_date",
    "page_count",
    "dimensions",
    "binding",
)


def write_gap_report(records, handle) -> int:
    """A per-book checklist of what is still missing.

    Written for use with the books in front of you: for limited editions most
    of these can only come off the limitation page, the imprint page or a
    ruler, so this doubles as a stocktaking sheet.
    """
    incomplete = 0
    for record in records:
        missing = [f for f in FROM_THE_BOOK if not getattr(record, f, "")]
        if not missing:
            continue
        incomplete += 1
        label = record.title or "(untitled)"
        handle.write(f"{record.isbn}  {label}\n")
        for name in missing:
            handle.write(f"    [ ] {name}\n")
        handle.write("\n")
    return incomplete
