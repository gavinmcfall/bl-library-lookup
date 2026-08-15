"""Source registry, ordered strongest-first."""

from __future__ import annotations

from .base import Candidate, Source
from .hardcover import Hardcover
from .hardcover import read_token as hardcover_token
from .openlibrary import GoogleBooks, OpenLibrary
from .sru import LibraryHubDiscover, NationalLibraryOfScotland

# Order matters: the resolver queries in this order and merges highest
# confidence first.
REGISTRY: dict[str, type[Source]] = {
    "nls": NationalLibraryOfScotland,
    "libraryhub": LibraryHubDiscover,
    "hardcover": Hardcover,
    "openlibrary": OpenLibrary,
    "googlebooks": GoogleBooks,
}

# Library Hub is excluded by default: it sits behind a bot challenge that
# returns 403 to scripted clients. Enable it explicitly with
# --sources nls,libraryhub,... if you have access. Hardcover is in the
# defaults but the CLI drops it when no token is present in the environment.
DEFAULT_SOURCES = ("nls", "hardcover", "openlibrary", "googlebooks")

__all__ = [
    "REGISTRY",
    "DEFAULT_SOURCES",
    "Source",
    "Candidate",
    "NationalLibraryOfScotland",
    "LibraryHubDiscover",
    "Hardcover",
    "hardcover_token",
    "OpenLibrary",
    "GoogleBooks",
]
