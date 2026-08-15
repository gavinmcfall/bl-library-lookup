"""Find archived Black Library product pages for a title.

The collectible half of the record -- print run, slipcase contents, cover
finish, original price -- never reaches a library catalogue. It lived on
blacklibrary.com product pages, which are gone from the live site but survive
in the Wayback Machine.

This is deliberately a *suggestion* helper, not a pipeline source: archived
product pages carry no ISBN, so a page can never pass the exact-ISBN match
that gates real sources. The honest flow is for a person to read the page,
confirm it describes the edition in hand, and copy the details into the
collectible columns of their shelf file.

Usage:
    python -m blmeta.wayback "War Storm"
    python -m blmeta.wayback "Corax" --max-pages 5
    blmeta-wayback "Mephiston"
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.parse
from pathlib import Path

from .http import Cache, FetchError, Fetcher

CDX_URL = "http://web.archive.org/cdx/search/cdx"

# Slugs that mark a product page as a special edition rather than a trade
# listing; used only for ranking, never for filtering pages out.
_SPECIAL_HINTS = ("limited", "exclusive", "collector", "deluxe", "ex-edition", "special")

DEFAULT_CACHE = Path.home() / ".cache" / "blmeta" / "responses.sqlite"


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug


def find_pages(fetcher: Fetcher, title: str, limit: int = 40) -> list[tuple[str, str]]:
    """CDX lookup: archived blacklibrary.com pages whose URL contains the title.

    Returns (timestamp, original_url) pairs, deduplicated by URL, special
    editions first. Wayback's CDX endpoint is slow and occasionally 504s;
    responses are cached so a retry or re-run costs nothing.
    """
    slug = slugify(title)
    params = {
        "url": "blacklibrary.com",
        "matchType": "domain",
        "output": "text",
        "collapse": "urlkey",
        "filter": f"urlkey:.*{re.escape(slug)}.*",
        "limit": str(limit),
    }
    query = f"{CDX_URL}?{urllib.parse.urlencode(params)}&filter=statuscode:200"
    response = fetcher.get(query)

    pages: list[tuple[str, str]] = []
    for line in response.body.splitlines():
        parts = line.split(" ")
        if len(parts) < 5:
            continue
        timestamp, original, mime = parts[1], parts[2], parts[3]
        if mime != "text/html":
            continue
        pages.append((timestamp, original))

    def rank(item: tuple[str, str]) -> tuple[int, str]:
        _, url = item
        lowered = url.lower()
        special = any(hint in lowered for hint in _SPECIAL_HINTS)
        return (0 if special else 1, url)

    return sorted(pages, key=rank)


def fetch_page(fetcher: Fetcher, timestamp: str, original: str) -> str:
    """Fetch the archived page body without the Wayback toolbar (id_ form)."""
    url = f"http://web.archive.org/web/{timestamp}id_/{original}"
    return fetcher.get(url).body


def extract(html: str) -> dict[str, object]:
    """Pull the collectible-relevant pieces out of a product page."""
    out: dict[str, object] = {}

    match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    if match:
        out["product_title"] = _clean(match.group(1))

    match = re.search(r"ABOUT THIS EDITION(.*?)(?:<h\d|ABOUT THE|THE STORY|$)", html, re.S | re.I)
    if match:
        # Page layouts vary across the years; the description is the first
        # paragraph with any substance, not merely the first <p> (which on
        # some layouts is a price element).
        for para in re.findall(r"<p[^>]*>(.*?)</p>", match.group(1), re.S):
            cleaned = _clean(para)
            if len(cleaned) > 60:
                out["about_edition"] = cleaned
                break

    text = _clean(re.sub(r"<[^>]+>", " ", html))

    match = re.search(
        r"(?:Fewer than|Only|Limited to)\s+[\d,]+\s+(?:copies|in stock|available)[^.]*",
        text,
        re.I,
    )
    if match:
        out["availability"] = match.group(0).strip()

    match = re.search(r"limited to\s+([\d,]+)\s+cop", text, re.I)
    if match:
        out["print_run"] = match.group(1).replace(",", "")

    if re.search(r"individually numbered|hand.numbered", text, re.I):
        out["numbered"] = True
    if re.search(r"\bsigned\b", text, re.I):
        out["mentions_signed"] = True

    prices = re.findall(r"[£$€]\s?\d+(?:\.\d{2})?", text)
    meaningful = [p.replace(" ", "") for p in prices if not p.rstrip("0.").endswith("0.")]
    meaningful = [p for p in dict.fromkeys(meaningful) if p not in ("$0.00", "£0.00", "€0.00")]
    if meaningful:
        out["prices_seen"] = meaningful[:5]

    return out


def _clean(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    value = value.replace("&amp;", "&").replace("&pound;", "£").replace("&euro;", "€")
    value = value.replace("&#39;", "'").replace("&quot;", '"').replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", value).strip()


def report(fetcher: Fetcher, title: str, max_pages: int) -> int:
    print(f"searching Wayback for blacklibrary.com pages matching '{title}'...",
          file=sys.stderr)
    try:
        pages = find_pages(fetcher, title)
    except FetchError as exc:
        print(f"CDX lookup failed: {exc}", file=sys.stderr)
        print("Wayback's CDX endpoint is flaky; retry in a minute.", file=sys.stderr)
        return 1

    if not pages:
        print(f"no archived pages found for '{title}'")
        return 0

    shown = 0
    for timestamp, original in pages:
        if shown >= max_pages:
            remaining = len(pages) - shown
            if remaining > 0:
                print(f"... and {remaining} more page(s); raise --max-pages to see them")
            break
        try:
            html = fetch_page(fetcher, timestamp, original)
        except FetchError as exc:
            print(f"  (snapshot fetch failed: {exc})", file=sys.stderr)
            continue
        details = extract(html)
        if not details:
            continue
        shown += 1

        print("=" * 72)
        print(f"{details.get('product_title', '(no title)')}")
        print(f"  archived: {timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}  {original}")
        print(f"  snapshot: http://web.archive.org/web/{timestamp}/{original}")
        if details.get("print_run"):
            print(f"  print_run: {details['print_run']}")
        if details.get("availability"):
            print(f"  availability: {details['availability']}")
        if details.get("numbered"):
            print("  numbered: yes (page mentions individual numbering)")
        if details.get("mentions_signed"):
            print("  signed: page mentions a signature (verify which edition)")
        if details.get("prices_seen"):
            print(f"  prices seen on page: {', '.join(details['prices_seen'])}")
        if details.get("about_edition"):
            print(f"  about this edition:\n    {details['about_edition']}")
        print()

    if shown:
        print(
            "These pages carry no ISBN, so nothing here is applied automatically.\n"
            "Confirm the page matches the edition in your hand, then copy the\n"
            "details into the collectible columns of your shelf file."
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="blmeta-wayback",
        description=(
            "Find archived blacklibrary.com product pages for a title and "
            "extract collectible-edition details (print run, contents, price) "
            "as suggestions to verify by hand."
        ),
    )
    parser.add_argument("title", help="book title to search for, e.g. 'War Storm'")
    parser.add_argument("--max-pages", type=int, default=3,
                        help="snapshots to fetch and summarise (default 3)")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE))
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--refresh", action="store_true",
                        help="ignore cached responses and refetch")
    parser.add_argument("--timeout", type=float, default=90.0,
                        help="per-request timeout; Wayback is slow (default 90)")
    args = parser.parse_args(argv)

    cache = None if args.no_cache else Cache(Path(args.cache))
    fetcher = Fetcher(cache=cache, delay=1.5, timeout=args.timeout, refresh=args.refresh)
    try:
        return report(fetcher, args.title, args.max_pages)
    finally:
        if cache:
            cache.close()


if __name__ == "__main__":
    raise SystemExit(main())
