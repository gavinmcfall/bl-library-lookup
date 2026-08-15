"""Command line interface for blmeta."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import isbn as isbn_utils
from .http import Cache, Fetcher
from .output import write_csv, write_json
from .record import ResolvedRecord, Status
from .resolver import Resolver
from .sources import DEFAULT_SOURCES, REGISTRY

DEFAULT_CACHE = Path.home() / ".cache" / "blmeta" / "responses.sqlite"


def read_isbns(args: argparse.Namespace) -> list[str]:
    """Collect ISBNs from positional arguments, a file, or stdin."""
    values: list[str] = list(args.isbns)
    if args.input:
        if str(args.input) == "-":
            text = sys.stdin.read()
        else:
            text = Path(args.input).read_text(encoding="utf-8")
        for line in text.splitlines():
            # Allow "# comments" and trailing notes after whitespace.
            line = line.split("#", 1)[0].strip()
            if line:
                values.append(line.split()[0])
    elif not values and not sys.stdin.isatty():
        for line in sys.stdin.read().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                values.append(line.split()[0])

    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        key = isbn_utils.canonical(value) or isbn_utils.clean(value)
        if key not in seen:
            seen.add(key)
            unique.append(value)
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

    isbns = read_isbns(args)
    if not isbns:
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
        resolver = Resolver(fetcher, source_names=source_names, raw_dir=raw_dir)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    records: list[ResolvedRecord] = []
    total = len(isbns)
    for index, value in enumerate(isbns, start=1):
        print(f"[{index}/{total}] {value.strip()}", file=sys.stderr, flush=True)
        record = resolver.resolve(value)
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
