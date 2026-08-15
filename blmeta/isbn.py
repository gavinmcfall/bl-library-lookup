"""ISBN normalisation, validation and conversion.

The whole resolver rests on treating the ISBN as the edition identity, so
everything here is deliberately strict: we never guess, and we never treat a
malformed ISBN as a near-miss for a valid one.
"""

from __future__ import annotations

import re

_STRIP = re.compile(r"[^0-9Xx]")

# Leading ISBN token inside a MARC 020 $a such as "9781784965297 (hbk.) : £18.00"
_LEADING_ISBN = re.compile(r"^\s*([0-9][0-9\- ]{8,}[0-9Xx])")

_BINDING_HINTS = (
    ("hbk", "hardback"),
    ("hardback", "hardback"),
    ("hardcover", "hardback"),
    ("cased", "hardback"),
    ("pbk", "paperback"),
    ("paperback", "paperback"),
    ("softback", "paperback"),
    ("ebook", "ebook"),
    ("electronic bk", "ebook"),
    ("epub", "ebook"),
    ("pdf", "ebook"),
    ("audio", "audio"),
    ("limited", "limited edition"),
    ("special", "special edition"),
)


def clean(value: str | None) -> str:
    """Reduce a string to bare ISBN characters (digits plus X)."""
    if not value:
        return ""
    return _STRIP.sub("", value).upper()


def is_valid_isbn10(value: str) -> bool:
    value = clean(value)
    if len(value) != 10:
        return False
    total = 0
    for index, char in enumerate(value):
        if char == "X":
            if index != 9:
                return False
            digit = 10
        elif char.isdigit():
            digit = int(char)
        else:
            return False
        total += digit * (10 - index)
    return total % 11 == 0


def is_valid_isbn13(value: str) -> bool:
    value = clean(value)
    if len(value) != 13 or not value.isdigit():
        return False
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(value))
    return total % 10 == 0


def to_isbn13(value: str) -> str | None:
    value = clean(value)
    if is_valid_isbn13(value):
        return value
    if not is_valid_isbn10(value):
        return None
    core = "978" + value[:9]
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core))
    return core + str((10 - total % 10) % 10)


def to_isbn10(value: str) -> str | None:
    value = clean(value)
    if is_valid_isbn10(value):
        return value
    isbn13 = to_isbn13(value)
    # Only the 978 range has an ISBN-10 equivalent.
    if not isbn13 or not isbn13.startswith("978"):
        return None
    core = isbn13[3:12]
    total = sum(int(c) * (10 - i) for i, c in enumerate(core))
    check = (11 - total % 11) % 11
    return core + ("X" if check == 10 else str(check))


def canonical(value: str) -> str | None:
    """Return the ISBN-13 form, or None when the input is not a valid ISBN."""
    return to_isbn13(value)


def variants(value: str) -> set[str]:
    """Every equivalent form of an ISBN, used for exact-match comparison."""
    found: set[str] = set()
    isbn13 = to_isbn13(value)
    if isbn13:
        found.add(isbn13)
    isbn10 = to_isbn10(value)
    if isbn10:
        found.add(isbn10)
    return found


def extract(value: str) -> str | None:
    """Pull the ISBN out of a qualified catalogue string.

    MARC 020 $a routinely carries trailing qualifiers and prices, e.g.
    ``9781784965297 (hbk.) : £18.00``. We take the leading token only.
    """
    if not value:
        return None
    match = _LEADING_ISBN.match(value)
    candidate = match.group(1) if match else value
    return canonical(candidate)


def binding_hint(value: str) -> str | None:
    """Infer a binding from an ISBN qualifier such as ``(hbk.)``."""
    if not value:
        return None
    lowered = value.lower()
    for needle, binding in _BINDING_HINTS:
        if needle in lowered:
            return binding
    return None


def matches(query: str, candidate: str) -> bool:
    """True only when two ISBNs denote the same edition.

    Comparison happens on the canonical ISBN-13 so that a source quoting the
    ISBN-10 still counts as an exact match -- but a different edition of the
    same title never will.
    """
    left, right = canonical(query), canonical(candidate)
    return bool(left and right and left == right)
