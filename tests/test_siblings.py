import argparse
import unittest

from blmeta.cli import read_inputs
from blmeta.record import Confidence, Status
from blmeta.resolver import Resolver, _same_work
from blmeta.sources.base import Candidate, Source

LIMITED = "9781784961480"
TRADE_HB = "9781784965297"
TRADE_PB = "9781784966669"

SIBLING_DATA = {
    "title": "Dante",
    "author": "Haley, Guy",
    "publisher": "Black Library",
    "publication_place": "Nottingham",
    "series": "Blood angels",
    "dewey": "823.92",
    "language": "English",
    # Edition-specific -- must never be inherited.
    "page_count": "304",
    "dimensions": "25 cm.",
    "binding": "hardback",
    "publication_date": "2017",
    "edition_statement": "First edition.",
}


class _NationalStub(Source):
    name = "national"
    confidence = Confidence.VERY_HIGH
    is_national_bibliography = True

    def __init__(self, siblings=None):
        super().__init__(None)
        self._siblings = siblings or []

    def search(self, isbn13):
        return []

    def search_siblings(self, title, author):
        return self._siblings


def _resolver(sources, inherit=False):
    resolver = Resolver.__new__(Resolver)
    resolver.fetcher = None
    resolver.raw_dir = None
    resolver.inherit_siblings = inherit
    resolver.sources = sources
    return resolver


class TestSameWork(unittest.TestCase):
    def test_rejects_title_containing_the_query(self):
        self.assertFalse(_same_work("Dante", "Accounting for Dante"))
        self.assertFalse(_same_work("Dante", "After Dante"))

    def test_accepts_subtitle_and_responsibility(self):
        self.assertTrue(_same_work("Dante", "Dante / Guy Haley."))
        self.assertTrue(_same_work("Dante", "Dante : a novel"))

    def test_ignores_leading_article_and_case(self):
        self.assertTrue(_same_work("The Emperor's Gift", "emperors gift"))


class TestSiblingIdentification(unittest.TestCase):
    def _siblings(self):
        return [Candidate({TRADE_HB}, dict(SIBLING_DATA))]

    def test_identifies_uncatalogued_edition(self):
        resolver = _resolver([_NationalStub(self._siblings())])
        record = resolver.resolve(LIMITED, user_data={"title": "Dante", "author": "Guy Haley"})
        self.assertEqual(record.status, Status.LIKELY_SPECIAL_EDITION)
        self.assertEqual(record.sibling_isbns, [TRADE_HB])

    def test_requires_a_title_to_search(self):
        resolver = _resolver([_NationalStub(self._siblings())])
        record = resolver.resolve(LIMITED)
        self.assertEqual(record.status, Status.NOT_IN_NATIONAL_BIBLIOGRAPHY)

    def test_own_isbn_never_listed_as_its_own_sibling(self):
        resolver = _resolver([_NationalStub([Candidate({LIMITED}, dict(SIBLING_DATA))])])
        record = resolver.resolve(LIMITED, user_data={"title": "Dante"})
        self.assertNotIn(LIMITED, record.sibling_isbns)

    def test_unrelated_title_is_not_a_sibling(self):
        wrong = [Candidate({TRADE_HB}, {"title": "Accounting for Dante"})]
        resolver = _resolver([_NationalStub(wrong)])
        record = resolver.resolve(LIMITED, user_data={"title": "Dante"})
        self.assertEqual(record.status, Status.NOT_IN_NATIONAL_BIBLIOGRAPHY)
        self.assertEqual(record.sibling_isbns, [])

    def test_sibling_data_does_not_leak_into_bibliographic_fields(self):
        """Without --inherit-siblings, a sibling describes nothing here."""
        resolver = _resolver([_NationalStub(self._siblings())])
        record = resolver.resolve(LIMITED, user_data={"title": "Dante"})
        self.assertEqual(record.publisher, "")
        self.assertEqual(record.page_count, "")
        self.assertEqual(record.dewey, "")


class TestSiblingInheritance(unittest.TestCase):
    def setUp(self):
        resolver = _resolver(
            [_NationalStub([Candidate({TRADE_HB}, dict(SIBLING_DATA))])], inherit=True
        )
        self.record = resolver.resolve(LIMITED, user_data={"title": "Dante"})

    def test_inherits_work_level_fields(self):
        self.assertEqual(self.record.publisher, "Black Library")
        self.assertEqual(self.record.series, "Blood angels")
        self.assertEqual(self.record.dewey, "823.92")

    def test_never_inherits_edition_specific_fields(self):
        for name in ("page_count", "dimensions", "binding", "publication_date",
                     "edition_statement"):
            self.assertEqual(
                getattr(self.record, name), "", f"{name} must not be inherited"
            )

    def test_provenance_names_the_source_isbn(self):
        self.assertEqual(
            self.record.field_provenance["publisher"], f"sibling:{TRADE_HB}"
        )

    def test_isbn_remains_the_queried_edition(self):
        self.assertEqual(self.record.isbn, LIMITED)


class TestUserSuppliedData(unittest.TestCase):
    def test_user_data_alone_is_not_resolved(self):
        resolver = _resolver([_NationalStub()])
        record = resolver.resolve(LIMITED, user_data={"limited_edition_number": "247/1500"})
        self.assertNotEqual(record.status, Status.RESOLVED)
        self.assertEqual(record.limited_edition_number, "247/1500")

    def test_user_title_beats_sibling_title(self):
        resolver = _resolver(
            [_NationalStub([Candidate({TRADE_HB}, dict(SIBLING_DATA))])], inherit=True
        )
        record = resolver.resolve(LIMITED, user_data={"title": "Dante", "author": "Guy Haley"})
        self.assertEqual(record.author, "Guy Haley")
        self.assertEqual(record.field_provenance["author"], "user")


class TestInputParsing(unittest.TestCase):
    @staticmethod
    def _args(**kwargs):
        return argparse.Namespace(isbns=[], input=None, **kwargs)

    def _read(self, text, tmp_path="/tmp/blmeta_test_input.txt"):
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(text)
        args = argparse.Namespace(isbns=[], input=tmp_path)
        return read_inputs(args)

    def test_plain_isbn_lines_with_comments(self):
        entries = self._read(f"# shelf 3\n{LIMITED}   # the LE\n\n{TRADE_HB}\n")
        self.assertEqual([e[0] for e in entries], [LIMITED, TRADE_HB])
        self.assertEqual(entries[0][1], {})

    def test_pipe_format_carries_copy_details(self):
        entries = self._read(f"{LIMITED} | Dante | Guy Haley | 247/1500 | 1500 | yes\n")
        isbn_value, data = entries[0]
        self.assertEqual(isbn_value, LIMITED)
        self.assertEqual(data["title"], "Dante")
        self.assertEqual(data["limited_edition_number"], "247/1500")
        self.assertEqual(data["print_run"], "1500")
        self.assertEqual(data["signed"], "yes")

    def test_pipe_format_allows_trailing_fields_to_be_omitted(self):
        entries = self._read(f"{LIMITED} | Dante\n")
        self.assertEqual(entries[0][1], {"title": "Dante"})

    def test_csv_input_with_header(self):
        entries = self._read(
            "isbn,title,author,copy_number\n"
            f"{LIMITED},Dante,Guy Haley,247/1500\n"
        )
        isbn_value, data = entries[0]
        self.assertEqual(isbn_value, LIMITED)
        self.assertEqual(data["limited_edition_number"], "247/1500")

    def test_duplicate_isbns_collapse(self):
        entries = self._read(f"{LIMITED}\n978-1-78496-148-0\n")
        self.assertEqual(len(entries), 1)


if __name__ == "__main__":
    unittest.main()
