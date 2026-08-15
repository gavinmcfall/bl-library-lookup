"""Command line interface for blmeta."""

from __future__ import annotations

import argparse
import csv
import io
import logging
import sys
from pathlib import Path

from . import isbn as isbn_utils
from .http import Cache, Fetcher
from .output import write_csv, write_gap_report, write_json
from .record import ResolvedRecord, Status
from .resolver import Resolver
from .sources import DEFAULT_SOURCES, REGISTRY

DEFAULT_CACHE = Path.home() / ".cache" / "blmeta" / "responses.sqlite"


# Pipe-delimited input columns, in order. Everything after the ISBN is
# optional, so "isbn | title" and the full form are both valid.
PIPE_FIELDS = (
    "title",
    "author",
    "limited_edition_number",
    "print_run",
    "signed",
    "notes",
)

# Accepted CSV header names -> record fields.
CSV_ALIASES = {
    "isbn": "isbn",
    "isbn13": "isbn",
    "title": "title",
    "author": "author",
    "limited_edition_number": "limited_edition_number",
    "edition_number": "limited_edition_number",
    "number": "limited_edition_number",
    "copy_number": "limited_edition_number",
    "print_run": "print_run",
    "signed": "signed",
    "notes": "notes",
    "special_contents": "special_contents",
    "cover_artist": "cover_artist",
    "product_code": "other_identifiers",
    "asin": "other_identifiers",
    "sibling_isbn": "declared_siblings",
    "sibling_isbns": "declared_siblings",
    "other_edition_isbn": "declared_siblings",
    "publisher": "publisher",
    "original_retail_price": "original_retail_price",
}


def _parse_line(line: str) -> tuple[str, dict[str, object]] | None:
    """Parse one input line into an ISBN plus any details you supplied."""
    line = line.split("#", 1)[0].strip()
    if not line:
        return None

    if "|" in line:
        parts = [part.strip() for part in line.split("|")]
        isbn_value, rest = parts[0], parts[1:]
        data: dict[str, object] = {}
        for name, value in zip(PIPE_FIELDS, rest):
            if value:
                data[name] = [value] if name == "notes" else value
        return isbn_value, data

    return line.split()[0], {}


def read_inputs(args: argparse.Namespace) -> list[tuple[str, dict[str, object]]]:
    """Collect ISBNs (and optional copy details) from arguments, file or stdin.

    Three input shapes are supported: a bare ISBN per line, a pipe-delimited
    line carrying details you read off the book, or a CSV with a header row.
    """
    entries: list[tuple[str, dict[str, object]]] = [(value, {}) for value in args.isbns]

    text = None
    if args.input:
        text = sys.stdin.read() if str(args.input) == "-" else Path(args.input).read_text(
            encoding="utf-8"
        )
    elif not entries and not sys.stdin.isatty():
        text = sys.stdin.read()

    if text is not None:
        lines = text.splitlines()
        header = lines[0].lower() if lines else ""
        is_csv = "," in header and "isbn" in header and "|" not in header

        if is_csv:
            for row in csv.DictReader(io.StringIO(text)):
                data: dict[str, object] = {}
                isbn_value = ""
                for key, value in row.items():
                    field = CSV_ALIASES.get((key or "").strip().lower())
                    if not field or not (value or "").strip():
                        continue
                    if field == "isbn":
                        isbn_value = value.strip()
                    elif field == "declared_siblings":
                        data[field] = [v.strip() for v in value.split(";") if v.strip()]
                    elif field == "other_identifiers":
                        # Several input columns feed this one field, so append
                        # rather than replace or only the last one survives.
                        data.setdefault(field, [])
                        data[field].append(f"{key.strip().lower()}:{value.strip()}")
                    elif field in ("notes", "special_contents"):
                        data[field] = [value.strip()]
                    else:
                        data[field] = value.strip()
                if isbn_value:
                    entries.append((isbn_value, data))
        else:
            for line in lines:
                parsed = _parse_line(line)
                if parsed:
                    entries.append(parsed)

    seen: set[str] = set()
    unique: list[tuple[str, dict[str, object]]] = []
    for value, data in entries:
        key = isbn_utils.canonical(value) or isbn_utils.clean(value)
        if key not in seen:
            seen.add(key)
            unique.append((value, data))
    return unique


def summarise(records: list[ResolvedRecord]) -> str:
    counts: dict[str, int] = {}
    for record in records:
        counts[record.status] = counts.get(record.status, 0) + 1
    parts = [f"{count} {status.lower().replace('_', ' ')}" for status, count in sorted(counts.items())]
    return ", ".join(parts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blmeta",
        description=(
            "Resolve Black Library (and other) ISBNs to bibliographic metadata "
            "from library catalogues, insisting on exact ISBN matches."
        ),
        epilog=(
            "Exit codes: 0 all resolved, 1 usage/IO error, "
            "2 one or more ISBNs unresolved (with --strict)."
        ),
    )
    parser.add_argument("isbns", nargs="*", help="ISBN-10 or ISBN-13 values to look up")
    parser.add_argument(
        "-i", "--input", help="file of ISBNs, one per line ('-' for stdin)"
    )
    parser.add_argument(
        "-o", "--output", help="write CSV here (default: stdout)"
    )
    parser.add_argument("--json-out", help="also write the full records as JSON")
    parser.add_argument(
        "--raw-dir", help="preserve raw MARCXML/JSON evidence under this directory"
    )
    parser.add_argument(
        "--sources",
        default=",".join(DEFAULT_SOURCES),
        help=f"comma-separated source list (available: {', '.join(REGISTRY)})",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="minimum seconds between requests to the same host (default: 1.0)",
    )
    parser.add_argument(
        "--timeout", type=float, default=45.0, help="per-request timeout in seconds"
    )
    parser.add_argument(
        "--cache",
        default=str(DEFAULT_CACHE),
        help=f"response cache path (default: {DEFAULT_CACHE})",
    )
    parser.add_argument(
        "--inherit-siblings",
        action="store_true",
        help=(
            "for uncatalogued editions, inherit work-level fields (author, "
            "publisher, series, classification) from a catalogued sibling; "
            "field_provenance records which ISBN each came from"
        ),
    )
    parser.add_argument(
        "--publisher-hint",
        default="Black Library",
        help=(
            "publisher used to narrow sibling searches when no author is known "
            "(default: 'Black Library'; pass '' to disable)"
        ),
    )
    parser.add_argument(
        "--gaps",
        help=(
            "write a per-book checklist of still-missing fields here; for "
            "limited editions most of these are only readable off the book"
        ),
    )
    parser.add_argument("--no-cache", action="store_true", help="disable the cache")
    parser.add_argument(
        "--refresh", action="store_true", help="ignore cached responses and refetch"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero if any ISBN fails to resolve",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING - min(args.verbose, 2) * 10,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    entries = read_inputs(args)
    if not entries:
        print("no ISBNs supplied (pass them as arguments or via --input)", file=sys.stderr)
        return 1

    source_names = tuple(s.strip() for s in args.sources.split(",") if s.strip())
    unknown = [name for name in source_names if name not in REGISTRY]
    if unknown:
        print(
            f"unknown source(s): {', '.join(unknown)}. Available: {', '.join(REGISTRY)}",
            file=sys.stderr,
        )
        return 1

    cache = None if args.no_cache else Cache(Path(args.cache))
    fetcher = Fetcher(
        cache=cache, delay=args.delay, timeout=args.timeout, refresh=args.refresh
    )
    raw_dir = Path(args.raw_dir) if args.raw_dir else None

    try:
        resolver = Resolver(
            fetcher,
            source_names=source_names,
            raw_dir=raw_dir,
            inherit_siblings=args.inherit_siblings,
            publisher_hint=args.publisher_hint,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    records: list[ResolvedRecord] = []
    total = len(entries)
    for index, (value, user_data) in enumerate(entries, start=1):
        print(f"[{index}/{total}] {value.strip()}", file=sys.stderr, flush=True)
        record = resolver.resolve(value, user_data=user_data)
        records.append(record)
        detail = record.title or record.warnings[0] if (record.title or record.warnings) else ""
        print(
            f"    -> {record.status}"
            + (f" ({record.confidence})" if record.confidence else "")
            + (f" :: {detail[:80]}" if detail else ""),
            file=sys.stderr,
            flush=True,
        )

    try:
        if args.output:
            with open(args.output, "w", encoding="utf-8", newline="") as handle:
                write_csv(records, handle)
            print(f"\nwrote {len(records)} rows to {args.output}", file=sys.stderr)
        else:
            write_csv(records, sys.stdout)

        if args.gaps:
            with open(args.gaps, "w", encoding="utf-8") as handle:
                incomplete = write_gap_report(records, handle)
            print(f"wrote gap checklist for {incomplete} book(s) to {args.gaps}", file=sys.stderr)

        if args.json_out:
            write_json(records, Path(args.json_out))
            print(f"wrote JSON to {args.json_out}", file=sys.stderr)
    except OSError as exc:
        print(f"cannot write output: {exc}", file=sys.stderr)
        return 1
    finally:
        if cache:
            cache.close()

    print(f"summary: {summarise(records)}", file=sys.stderr)

    if args.strict and any(r.status != Status.RESOLVED for r in records):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
