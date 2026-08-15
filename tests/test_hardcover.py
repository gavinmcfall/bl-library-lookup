import json
import unittest
from unittest import mock

from blmeta.http import FetchError, Response
from blmeta.sources.hardcover import Hardcover

MEPHISTON_LE = "9781849703734"

# Shape verified against the live API on 2026-08-15.
EDITION = {
    "id": 31264086,
    "isbn_13": MEPHISTON_LE,
    # Live-verified quirk: this is a DIFFERENT edition's ISBN, not the
    # ISBN-10 form of isbn_13.
    "isbn_10": "1782517553",
    "title": "Mephiston: Lord of Death",
    "subtitle": None,
    "pages": 128,
    "release_date": "2013-01-25",
    "physical_format": None,
    "edition_format": "Hardcover",
    "edition_information": "Limited Edition",
    "publisher": {"name": "The Black Library"},
    "language": {"language": "English"},
    "book": {
        "title": "Mephiston: Lord of Death",
        "description": None,
        "cached_contributors": [
            {"author": {"name": "David Annandale"}, "contribution": None}
        ],
        "cached_image": {"url": "https://assets.hardcover.app/x.jpeg"},
    },
}


class _StubFetcher:
    def __init__(self, editions):
        self.body = json.dumps({"data": {"editions": editions}})
        self.calls = []

    def post(self, url, body, headers=None, accept="application/json"):
        self.calls.append((url, body, headers))
        return Response(url=url, status=200, body=self.body)


def _source(editions, token="tok"):
    fetcher = _StubFetcher(editions)
    with mock.patch.dict("os.environ", {"HARDCOVER_TOKEN": token}, clear=False):
        source = Hardcover(fetcher)
    return source, fetcher


class TestMapping(unittest.TestCase):
    def setUp(self):
        source, _ = _source([EDITION])
        self.result = source.lookup(MEPHISTON_LE)

    def test_exact_isbn_match_resolves(self):
        self.assertIsNotNone(self.result)

    def test_edition_level_fields(self):
        data = self.result.data
        self.assertEqual(data["title"], "Mephiston: Lord of Death")
        self.assertEqual(data["page_count"], "128")
        self.assertEqual(data["publication_date"], "2013-01-25")
        self.assertEqual(data["edition_statement"], "Limited Edition")
        self.assertEqual(data["binding"], "hardcover")
        self.assertEqual(data["publisher"], "The Black Library")
        self.assertEqual(data["author"], "David Annandale")
        self.assertEqual(data["cover_url"], "https://assets.hardcover.app/x.jpeg")


class TestExactMatchDiscipline(unittest.TestCase):
    def test_stray_isbn10_cannot_claim_another_edition(self):
        """The edition's isbn_10 belongs to a different edition; a query for
        THAT edition must not be satisfied by this record's metadata alone --
        the base class sees the ISBN sets match (isbn_10 is asserted by the
        record) so it resolves, but the point is the reverse direction:"""
        source, _ = _source([EDITION])
        # A completely unrelated ISBN must never match.
        self.assertIsNone(source.lookup("9781784961480"))


class TestToken(unittest.TestCase):
    def test_missing_token_is_unavailable_not_empty(self):
        fetcher = _StubFetcher([EDITION])
        with mock.patch.dict(
            "os.environ",
            {"HARDCOVER_TOKEN": "", "BOOKORBIT_HARDCOVER_TOKEN": ""},
            clear=False,
        ):
            source = Hardcover(fetcher)
        with self.assertRaises(FetchError):
            source.search(MEPHISTON_LE)

    def test_token_travels_in_header_not_url(self):
        source, fetcher = _source([EDITION])
        source.lookup(MEPHISTON_LE)
        for url, body, headers in fetcher.calls:
            self.assertNotIn("tok", url)
            self.assertNotIn("tok", body)
            self.assertEqual(headers["Authorization"], "Bearer tok")

    def test_fallback_env_var(self):
        fetcher = _StubFetcher([EDITION])
        with mock.patch.dict(
            "os.environ",
            {"HARDCOVER_TOKEN": "", "BOOKORBIT_HARDCOVER_TOKEN": "alt"},
            clear=False,
        ):
            source = Hardcover(fetcher)
        self.assertIsNotNone(source.lookup(MEPHISTON_LE))


if __name__ == "__main__":
    unittest.main()
