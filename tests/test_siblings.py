import argparse
import unittest

from blmeta.cli import read_inputs
from blmeta.record import Confidence, Status
from blmeta.resolver import Resolver, _collects_work, _same_work
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

    def search_siblings(self, title, author="", publisher=""):
        return self._siblings


def _resolver(sources, inherit=False, publisher_hint="Black Library,Games Workshop"):
    resolver = Resolver.__new__(Resolver)
    resolver.fetcher = None
    resolver.raw_dir = None
    resolver.inherit_siblings = inherit
    resolver.publisher_hint = publisher_hint
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


class TestIsbnLessSiblingRecord(unittest.TestCase):
    """A catalogued record with no ISBN still identifies an uncatalogued edition.

    Reissues and partworks are often catalogued without an ISBN. Such a record
    cites no sibling ISBN, but it does prove the work is catalogued while the
    edition in hand is not -- which is the whole inference.
    """

    def setUp(self):
        record_data = dict(SIBLING_DATA)
        record_data["publisher"] = "Black Library"
        self.candidate = Candidate(set(), record_data, record_id="99123")

    def test_identifies_without_any_sibling_isbn(self):
        resolver = _resolver([_NationalStub([self.candidate])])
        record = resolver.resolve(LIMITED, user_data={"title": "Dante"})
        self.assertEqual(record.status, Status.LIKELY_SPECIAL_EDITION)
        self.assertEqual(record.sibling_isbns, [])
        self.assertTrue(record.sibling_editions)
        self.assertIn("no ISBN", record.sibling_editions[0])

    def test_still_inherits_work_level_fields(self):
        resolver = _resolver([_NationalStub([self.candidate])], inherit=True)
        record = resolver.resolve(LIMITED, user_data={"title": "Dante"})
        self.assertEqual(record.series, "Blood angels")
        self.assertEqual(record.field_provenance["series"], "sibling:99123")


class TestIssueLevelFieldsNotBorrowedAcrossPublishers(unittest.TestCase):
    """A reissue by another house must not lend its imprint to this edition."""

    def _record(self, publisher, hint="Black Library"):
        data = dict(SIBLING_DATA)
        data["publisher"] = publisher
        data["publication_place"] = "London"
        resolver = _resolver(
            [_NationalStub([Candidate({TRADE_HB}, data)])], inherit=True, publisher_hint=hint
        )
        return resolver.resolve(LIMITED, user_data={"title": "Dante"})

    def test_different_publisher_withholds_imprint(self):
        record = self._record("Hachette Partworks Ltd")
        self.assertEqual(record.publisher, "")
        self.assertEqual(record.publication_place, "")

    def test_different_publisher_still_lends_work_level_fields(self):
        record = self._record("Hachette Partworks Ltd")
        self.assertEqual(record.series, "Blood angels")
        self.assertEqual(record.dewey, "823.92")

    def test_different_publisher_is_reported(self):
        record = self._record("Hachette Partworks Ltd")
        self.assertTrue(
            any("not inherited" in w for w in record.warnings),
            "the withheld imprint must be explained",
        )

    def test_same_publisher_lends_imprint(self):
        record = self._record("Black Library")
        self.assertEqual(record.publisher, "Black Library")
        self.assertEqual(record.publication_place, "London")

    def test_no_hint_means_no_restriction(self):
        record = self._record("Hachette Partworks Ltd", hint="")
        self.assertEqual(record.publisher, "Hachette Partworks Ltd")


class TestCollectionDetection(unittest.TestCase):
    """A novella may survive in print only inside an omnibus.

    The omnibus is a different work, so it is never a sibling edition -- but
    recording where the text can be found is useful, and its presence still
    shows the work was published while this ISBN went uncatalogued.
    """

    OMNIBUS = Candidate(
        {"9781784961534", "9781784961541"},
        {
            "title": "Flesh tearers",
            "subtitle": "flesh of Cretacia : Sons of Wrath : Trial by blood",
            "publication_date": "2016",
            "publisher": "Black Library",
        },
    )

    def test_detects_collected_work(self):
        self.assertTrue(_collects_work("Sons of Wrath", self.OMNIBUS.data))

    def test_matches_whole_segments_only(self):
        """A stray substring must not count as a collected title."""
        self.assertFalse(_collects_work("Wrath", self.OMNIBUS.data))
        self.assertFalse(_collects_work("Blood", self.OMNIBUS.data))

    def test_reads_505_contents_note(self):
        data = {"title": "Omnibus", "contents": "Dante -- Ahriman -- Corax"}
        self.assertTrue(_collects_work("Ahriman", data))

    def test_records_collection_and_identifies_edition(self):
        resolver = _resolver([_NationalStub([self.OMNIBUS])])
        record = resolver.resolve(LIMITED, user_data={"title": "Sons of Wrath"})
        self.assertEqual(record.status, Status.LIKELY_SPECIAL_EDITION)
        self.assertTrue(record.collected_in)
        self.assertIn("9781784961534", record.collected_in[0])

    def test_collection_is_never_a_sibling_edition(self):
        resolver = _resolver([_NationalStub([self.OMNIBUS])])
        record = resolver.resolve(LIMITED, user_data={"title": "Sons of Wrath"})
        self.assertEqual(record.sibling_isbns, [])

    def test_collection_lends_no_metadata(self):
        """An omnibus describes itself, not the novella in hand."""
        resolver = _resolver([_NationalStub([self.OMNIBUS])], inherit=True)
        record = resolver.resolve(LIMITED, user_data={"title": "Sons of Wrath"})
        self.assertEqual(record.title, "Sons of Wrath")
        self.assertEqual(record.publisher, "")
        self.assertEqual(record.publication_date, "")


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

    def _read(self, text):
        import os
        import tempfile

        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".txt", delete=False
        )
        try:
            handle.write(text)
            handle.close()
            args = argparse.Namespace(isbns=[], input=handle.name)
            return read_inputs(args)
        finally:
            os.unlink(handle.name)

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


class TestDeclaredSiblings(unittest.TestCase):
    """Sibling ISBNs you supply are verified, not believed."""

    class _Verifier(_NationalStub):
        def __init__(self, known):
            super().__init__([])
            self._known = known

        def search(self, isbn13):
            data = self._known.get(isbn13)
            return [Candidate({isbn13}, data)] if data else []

    def _resolve(self, declared, known=None):
        known = known or {TRADE_HB: {"title": "Dante", "publication_date": "2017",
                                     "publisher": "Black Library", "series": "Blood angels"}}
        resolver = _resolver([self._Verifier(known)], inherit=True)
        return resolver.resolve(
            LIMITED, user_data={"title": "Dante", "declared_siblings": declared}
        )

    def test_confirmed_sibling_identifies_edition(self):
        record = self._resolve([TRADE_HB])
        self.assertEqual(record.status, Status.LIKELY_SPECIAL_EDITION)
        self.assertEqual(record.sibling_isbns, [TRADE_HB])
        self.assertIn("Dante", record.sibling_editions[0])

    def test_confirmed_sibling_lends_work_level_fields(self):
        record = self._resolve([TRADE_HB])
        self.assertEqual(record.series, "Blood angels")
        self.assertEqual(record.field_provenance["series"], f"sibling:{TRADE_HB}")

    def test_unconfirmed_sibling_is_marked_not_dropped(self):
        record = self._resolve([TRADE_PB])
        self.assertIn(TRADE_PB, record.sibling_isbns)
        self.assertIn("unverified", record.sibling_editions[0])

    def test_invalid_declared_sibling_is_rejected(self):
        record = self._resolve(["not-an-isbn"])
        self.assertEqual(record.sibling_isbns, [])
        self.assertTrue(any("not a valid ISBN" in w for w in record.warnings))

    def test_self_declared_sibling_is_ignored(self):
        record = self._resolve([LIMITED])
        self.assertEqual(record.sibling_isbns, [])
        self.assertTrue(any("own ISBN" in w for w in record.warnings))


class TestPublisherAliases(unittest.TestCase):
    """One house catalogues under several names."""

    def _publisher(self, sibling_publisher, hint="Black Library,Games Workshop"):
        data = dict(SIBLING_DATA)
        data["publisher"] = sibling_publisher
        resolver = _resolver(
            [_NationalStub([Candidate({TRADE_HB}, data)])], inherit=True, publisher_hint=hint
        )
        return resolver.resolve(LIMITED, user_data={"title": "Dante"}).publisher

    def test_games_workshop_counts_as_black_library(self):
        self.assertEqual(self._publisher("Games Workshop, Limited"), "Games Workshop, Limited")

    def test_black_library_still_matches(self):
        self.assertEqual(self._publisher("Black Library"), "Black Library")

    def test_unrelated_house_still_withheld(self):
        self.assertEqual(self._publisher("Hachette Partworks Ltd"), "")


class TestSecondaryHitStillEnriched(unittest.TestCase):
    """An exact-ISBN hit in a secondary source must not suppress library
    enrichment: the national catalogue still knows the work's siblings."""

    class _SecondaryHit(Source):
        name = "secondary"
        confidence = Confidence.MEDIUM

        def search(self, isbn13):
            return [Candidate({isbn13}, {"title": "Dante",
                                         "publication_date": "2017-03-21",
                                         "edition_statement": "Limited Edition"})]

    def setUp(self):
        national = _NationalStub([Candidate({TRADE_HB}, dict(SIBLING_DATA))])
        resolver = _resolver([national, self._SecondaryHit(None)], inherit=True)
        self.record = resolver.resolve(LIMITED, user_data={"title": "Dante"})

    def test_status_stays_resolved(self):
        self.assertEqual(self.record.status, Status.RESOLVED)

    def test_edition_data_from_secondary_wins(self):
        self.assertEqual(self.record.edition_statement, "Limited Edition")
        self.assertEqual(self.record.publication_date, "2017-03-21")

    def test_work_level_fields_inherited_from_sibling(self):
        self.assertEqual(self.record.series, "Blood angels")
        self.assertEqual(self.record.dewey, "823.92")

    def test_sibling_isbns_recorded(self):
        self.assertEqual(self.record.sibling_isbns, [TRADE_HB])
