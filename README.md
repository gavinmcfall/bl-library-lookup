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
blmeta --input my-shelf.txt --output collection.csv --inherit-siblings
```

### Input formats

**Bare ISBNs**, one per line. `#` comments are stripped, so annotate freely:

```
9781784965297   # Dante, hardback, shelf 3
9781849708500
```

**Pipe-delimited**, when you are holding a limited edition and want its
copy-specific details recorded. Fields are
`isbn | title | author | edition_number | print_run | signed | notes`, and you
can stop at any point:

```
9781784961480 | Dante | Guy Haley | 247/1500 | 1500 | yes, signed by author
9781784965297 | Dante
```

**CSV with a header**, if you would rather work in a spreadsheet. Recognised
columns include `isbn`, `title`, `author`, `copy_number`/`edition_number`,
`print_run`, `signed`, `notes`, `product_code`, `asin`, and `sibling_isbn`:

```csv
isbn,title,author,copy_number,signed,sibling_isbn
9781784961480,Dante,Guy Haley,247/1500,yes,9781784965297;9781784966669
```

`sibling_isbn` takes a semicolon-separated list of other editions of the same
work. Knowing the trade edition's ISBN identifies a limited edition far more
directly than any title search — but each one is still looked up by exact
ISBN, so a wrong or mistyped number is reported as `unverified` rather than
believed.

Anything you supply yourself outranks every online source. For copy-specific
facts that is simply correct: no database on earth knows your copy is №247.

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
| `--inherit-siblings` | fill work-level fields on uncatalogued editions from a catalogued sibling |
| `--publisher-hint` | comma-separated names treated as one house (default `Black Library,Games Workshop`) |
| `--gaps` | write a per-book checklist of still-missing fields |
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
| Hardcover | `MEDIUM` | Community database, GraphQL. Holds edition-level records for several Black Library limited editions the libraries lack — `edition_information: "Limited Edition"`, page counts, release dates. Needs a bearer token in `HARDCOVER_TOKEN` (or `BOOKORBIT_HARDCOVER_TOKEN`); skipped from the defaults when absent. |
| Open Library | `MEDIUM` | Good for covers and page counts. |
| Google Books | `MEDIUM` | Heavily rate-limited; frequently returns HTTP 429. |

Hardcover's token travels only in the `Authorization` header — never in a URL —
so it cannot reach the on-disk response cache. Supply it from your secret
manager, e.g. `$env:HARDCOVER_TOKEN = op read "op://vault/item/field"`.

A secondary-source exact-ISBN hit does not suppress library enrichment: when
the national bibliography lacks the ISBN but a secondary source has it, the
sibling search still runs so work-level fields (series, classification) and
sibling ISBNs are filled from the catalogue alongside the edition-level data.

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
| `LIKELY_SPECIAL_EDITION` | Not catalogued under this ISBN, but sibling editions of the same work are. **This is how a limited edition is identified.** |
| `NOT_IN_NATIONAL_BIBLIOGRAPHY` | Valid ISBN, the national source answered, and it holds no such record — and no siblings were found (or no title was supplied to search for them). |
| `UNRESOLVED` | Nothing found, but a source was unreachable — absence is *not* proven. |
| `INVALID_ISBN` | Failed checksum or length. |

The distinction between the middle two is the point. A blocked or rate-limited
source never gets reported as "this book doesn't exist".

### Confidence

`VERY_HIGH` › `HIGH` › `MEDIUM_HIGH` › `MEDIUM` › `LOW` › `VERY_LOW`

Note that catalogue-in-publication (CIP) data ranks *below* a published
catalogue record. CIP is supplied by the publisher before the book exists, so
its titles, page counts and dates are provisional and are often superseded.

## Identifying limited editions

Black Library limited editions frequently have **no catalogue record at all**.
Running the three *Dante* ISBNs bare:

```
9781784965297 -> RESOLVED (VERY_HIGH)
9781784966669 -> RESOLVED (VERY_HIGH)
9781784961480 -> NOT_IN_NATIONAL_BIBLIOGRAPHY
```

The trade editions are catalogued. The Limited Edition is not. So how do you
identify it? By its **siblings**.

Give the LE a title, and `blmeta` searches the catalogue for other editions of
the same work:

```
9781784961480 | Dante | Guy Haley | 247/1500 | 1500 | yes, signed
```

```
-> LIKELY_SPECIAL_EDITION
   sibling_isbns:    9781784965297 | 9781784966669
   sibling_editions: 9781784965297 (2017 hardback) | 9781784966669 (2018)
```

A valid publisher ISBN with **no catalogue record of its own**, sitting
alongside **catalogued siblings of the same work**, is the signature of an
uncatalogued special edition. That inference is what the status records.

A title alone is too weak a query to find siblings: searching NLS for "Dante"
returns 1,700+ records and the real book is nowhere near the first page. So the
search pairs the title with a second term, trying author, then publisher
(`--publisher-hint`, defaulting to `Black Library`), then title alone. A wrong
hint costs nothing — it returns no results and falls through to the next
strategy.

Sibling metadata lands in the `sibling_*` columns and never in the
bibliographic ones — it describes different ISBNs. With `--inherit-siblings`
you can additionally fill in the fields that genuinely belong to the *work*
rather than the edition:

- **Inherited:** author, publisher, imprint, place, series, subjects, Dewey,
  LC classification, language.
- **Never inherited:** page count, dimensions, binding, edition statement,
  publication date, price, cover — these differ between editions by definition.

Publisher, imprint and place describe the *issue* rather than the work, so they
are inherited only from a sibling published by the same house. "Same house" is
judged against `--publisher-hint`, which takes a list because one publisher
catalogues under several names — Black Library is Games Workshop's fiction
imprint, and records appear under both. A sibling can be
a reissue by someone else — a Hachette partwork of a Black Library novel, say —
and stamping that imprint on your edition would misdescribe it. When those
fields are withheld, the reason is recorded in `warnings`.

A catalogued record with **no ISBN of its own** still counts as identification
evidence. Reissues and partworks are frequently catalogued without one; such a
record cites no sibling ISBN, but it does establish that the work is catalogued
while the edition in hand is not — which is the entire inference.

Every inherited value is stamped in `field_provenance` with the ISBN it came
from (`publisher=sibling:9781784965297`), so nothing is ever silently passed
off as observed on the edition in your hand.

### Most of what is still missing is on the book, not online

Run with `--gaps` for a per-book checklist. For limited editions nearly every
remaining field is a fact about the physical object rather than about the
edition, and no publisher, retailer or library holds it:

| Field | Where it actually is |
|---|---|
| `print_run`, `limited_edition_number` | the limitation page ("this is copy 247 of 1500") |
| `signed` | the signature page |
| `cover_artist` | the credits or imprint page |
| `publication_date` | the imprint page |
| `page_count`, `dimensions`, `binding` | the book itself |

### Your copy number can only come from you

`limited_edition_number`, `print_run` and `signed` are not in any database.
№247 of 1500 is a fact about the physical object on your shelf. Type it in as
you catalogue — user-supplied data outranks every online source, which for
these fields is the only correct ranking.

### On the CIP statement

It is tempting to read the imprint page — "A CIP record for this book is
available from the British Library" — as proof that a record exists for *that*
edition. It isn't. That line is boilerplate carried on the imprint page of
essentially every UK trade book, and a limited edition is usually typeset from
the same imprint page as the trade edition. Publishers often never submit
separate CIP for a special edition, so the statement is inherited text rather
than evidence about the ISBN in your hand.

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
