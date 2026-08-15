# blmeta

Bulk ISBN → CSV metadata lookup for Black Library books, built around one rule:
**the ISBN is the edition identity.** A title/author match is never accepted as
a substitute.

That rule matters because Black Library publishes the same novel in trade,
paperback and limited/special editions. Guy Haley's *Dante* exists as at least
three separate books:

| ISBN | Edition |
|---|---|
| `9781784961480` | Limited Edition |
| `9781784965297` | 2017 hardback |
| `9781784966669` | 2018 paperback |

Any tool that searches "Dante Guy Haley" and takes the first hit will silently
give you the wrong book. `blmeta` will return nothing before it returns the
wrong edition.

## Install

No dependencies — standard library only, Python 3.9+.

```bash
git clone https://github.com/gavinmcfall/bl-library-lookup
cd bl-library-lookup
pip install -e .          # provides the `blmeta` command
```

Or run it straight from the checkout without installing:

```bash
python3 -m blmeta.cli 9781784965297
```

## Usage

Cataloguing a stack of books — the main use case:

```bash
blmeta --input my-shelf.txt --output collection.csv
```

`my-shelf.txt` is one ISBN per line; `#` comments and trailing notes are
ignored, so you can annotate as you type them in:

```
9781784965297   # Dante, hardback, shelf 3
9781849708500
```

Other options:

```bash
blmeta 9781784961480                          # single lookup to stdout
blmeta -i shelf.txt -o out.csv --json-out out.json --raw-dir raw/
blmeta -i shelf.txt -o out.csv --sources nls  # library data only
blmeta -i shelf.txt -o out.csv --refresh      # bypass the cache
```

| Flag | Effect |
|---|---|
| `-i, --input` | ISBN file, one per line (`-` for stdin) |
| `-o, --output` | CSV destination (default stdout) |
| `--json-out` | full records as JSON, including nested provenance |
| `--raw-dir` | preserve raw MARCXML/JSON evidence per ISBN |
| `--sources` | pick sources: `nls,libraryhub,openlibrary,googlebooks` |
| `--delay` | seconds between requests to one host (default 1.0) |
| `--refresh` / `--no-cache` | bypass or disable the response cache |
| `--strict` | exit `2` if anything failed to resolve |

Responses are cached in `~/.cache/blmeta/responses.sqlite` for 30 days, so
re-running over the same shelf is instant and costs the catalogues nothing.

## Sources

Queried strongest-first, and merged field-by-field so the most authoritative
source wins each field while weaker ones fill the gaps.

| Source | Confidence | Notes |
|---|---|---|
| National Library of Scotland | `VERY_HIGH` | BNB-derived MARC over SRU. Records carry BL control numbers (`035 (Uk)...`). |
| Library Hub Discover | `MEDIUM_HIGH` | UK union catalogue. **Off by default** — bot-challenged, returns 403 to scripts. |
| Open Library | `MEDIUM` | Good for covers and page counts. |
| Google Books | `MEDIUM` | Heavily rate-limited; frequently returns HTTP 429. |

### Why SRU and not Z39.50

NLS does expose Z39.50 on port 1921, but the same catalogue index is available
over plain HTTPS via SRU. SRU needs no `yaz-client` binary and works through
proxies and firewalls that block raw TCP on non-standard ports. It is the same
data with fewer moving parts.

## Output

One row per ISBN. Columns are grouped:

- **Bibliographic** — `title`, `author`, `publisher`, `publication_date`,
  `edition_statement`, `page_count`, `series`, `dewey`, `bnb_number`, and so on.
- **Collectible** — `limited_edition_number`, `print_run`, `signed`,
  `special_contents`, `cover_artist`, `original_retail_price`. Kept separate on
  purpose: library records describe the bibliographic edition, not the
  collectible object. `blmeta` fills these only from explicit catalogue
  evidence (an edition statement or note); otherwise they are left blank for
  you to complete by hand.
- **Provenance** — `status`, `confidence`, `metadata_sources`,
  `source_record_ids`, `field_provenance` (which source won each field),
  `date_retrieved`, `warnings`.

### Status values

| Status | Meaning |
|---|---|
| `RESOLVED` | An exact-ISBN record was found. |
| `NOT_IN_NATIONAL_BIBLIOGRAPHY` | Valid ISBN, the national source answered, and it holds no such record. **Proven absence.** |
| `UNRESOLVED` | Nothing found, but a source was unreachable — absence is *not* proven. |
| `INVALID_ISBN` | Failed checksum or length. |

The distinction between the middle two is the point. A blocked or rate-limited
source never gets reported as "this book doesn't exist".

### Confidence

`VERY_HIGH` › `HIGH` › `MEDIUM_HIGH` › `MEDIUM` › `LOW` › `VERY_LOW`

Note that catalogue-in-publication (CIP) data ranks *below* a published
catalogue record. CIP is supplied by the publisher before the book exists, so
its titles, page counts and dates are provisional and are often superseded.

## Expect limited editions to come back empty

This is the single most important thing to know before you run it on a
collection of special editions.

Black Library limited editions frequently have **no catalogue record at all**.
Running the three *Dante* ISBNs:

```
9781784965297 -> RESOLVED (VERY_HIGH)
9781784966669 -> RESOLVED (VERY_HIGH)
9781784961480 -> NOT_IN_NATIONAL_BIBLIOGRAPHY
```

The trade editions are catalogued. The Limited Edition is not.

It is tempting to read the imprint page — "A CIP record for this book is
available from the British Library" — as proof that a record exists for *that*
edition. It isn't. That line is boilerplate carried on the imprint page of
essentially every UK trade book, and a limited edition is usually typeset from
the same imprint page as the trade edition. Publishers often never submit
separate CIP for a special edition, so the statement is inherited text rather
than evidence about the ISBN in your hand.

When you get `NOT_IN_NATIONAL_BIBLIOGRAPHY`, the practical route is:

1. Look up the **trade edition's** ISBN for shared bibliographic data (author,
   series, publication year, Dewey) — most of it is common to both editions.
2. Get edition-specific details (print run, numbering, slipcase, signature)
   from archived Black Library product pages, Warhammer Community
   announcements, or collector archives, and record them in the collectible
   columns by hand.

Keeping those two steps separate is why the CSV separates the two column
groups. Do not let step 2 overwrite step 1.

### A note on the British Library

The NLS is not the British Library. It receives legal deposit material through
the Agency for Legal Deposit Libraries, so absence from NLS does not prove
absence from the BL's own catalogue. The BL's linked-data service
(`bnb.data.bl.uk`) closed in March 2022 and its replacement is not currently
queryable by script, which is why NLS is used as the BNB-derived proxy here.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests cover ISBN validation and conversion, MARC parsing, CSV output, and —
most importantly — the exact-match discipline: that a record for one *Dante*
edition can never satisfy a query for another.

## Licence

MIT
