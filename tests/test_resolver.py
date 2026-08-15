import io
import unittest

from blmeta import marc
from blmeta.output import write_csv
from blmeta.record import Confidence, SourceRecord, Status, merge
from blmeta.resolver import Resolver
from blmeta.sources.base import Candidate, Source
from blmeta.http import FetchError

LIMITED = "9781784961480"
TRADE = "9781784965297"

MARCXML = """<?xml version="1.0"?>
<searchRetrieveResponse xmlns="http://www.loc.gov/zing/srw/">
 <records><record><recordData>
  <record xmlns="http://www.loc.gov/MARC21/slim">
   <controlfield tag="001">99123</controlfield>
   <controlfield tag="008">170101s2017    enk           000 1 eng d</controlfield>
   <datafield tag="020"><subfield code="a">9781784965297 (hbk.) :</subfield>
     <subfield code="c">£18.00</subfield></datafield>
   <datafield tag="020"><subfield code="z">9781784961480</subfield></datafield>
   <datafield tag="035"><subfield code="a">(Uk)018172334</subfield></datafield>
   <datafield tag="100"><subfield code="a">Haley, Guy,</subfield>
     <subfield code="d">1973-</subfield><subfield code="e">author.</subfield></datafield>
   <datafield tag="245"><subfield code="a">Dante /</subfield>
     <subfield code="c">Guy Haley.</subfield></datafield>
   <datafield tag="250"><subfield code="a">Limited edition.</subfield></datafield>
   <datafield tag="264" ind2="1"><subfield code="a">Nottingham :</subfield>
     <subfield code="b">Black Library,</subfield><subfield code="c">2017.</subfield></datafield>
   <datafield tag="300"><subfield code="a">304 pages ;</subfield>
     <subfield code="c">25 cm.</subfield></datafield>
   <datafield tag="490"><subfield code="a">Blood angels</subfield></datafield>
   <datafield tag="830"><subfield code="a">Blood Angels.</subfield></datafield>
   <datafield tag="082"><subfield code="a">823.92</subfield></datafield>
  </record>
 </recordData></record></records>
</searchRetrieveResponse>"""


class TestMarcParsing(unittest.TestCase):
    def setUp(self):
        self.record = marc.iter_records(MARCXML)[0]

    def test_extracts_core_fields(self):
        data = marc.parse(self.record)
        self.assertEqual(data["title"], "Dante")
        self.assertEqual(data["author"], "Haley, Guy")
        self.assertEqual(data["publisher"], "Black Library")
        self.assertEqual(data["publication_place"], "Nottingham")
        self.assertEqual(data["publication_date"], "2017")
        self.assertEqual(data["page_count"], "304")
        self.assertEqual(data["dimensions"], "25 cm.")
        self.assertEqual(data["edition_statement"], "Limited edition.")
        self.assertEqual(data["binding"], "hardback")
        self.assertEqual(data["dewey"], "823.92")
        self.assertEqual(data["bnb_number"], "018172334")
        self.assertEqual(data["language"], "English")
        self.assertEqual(data["original_retail_price"], "£18.00")

    def test_series_deduplicated_across_490_and_830(self):
        self.assertEqual(marc.parse(self.record)["series"], "Blood angels")

    def test_cancelled_isbn_subfield_z_is_ignored(self):
        """$z is a cancelled/invalid ISBN and must never claim an edition."""
        isbns = marc.record_isbns(self.record)
        self.assertIn(TRADE, isbns)
        self.assertNotIn(LIMITED, isbns)


class _StubSource(Source):
    name = "stub"
    confidence = Confidence.MEDIUM

    def __init__(self, candidates, fetcher=None, error=None):
        super().__init__(fetcher)
        self._candidates = candidates
        self._error = error

    def search(self, isbn13):
        if self._error:
            raise self._error
        return self._candidates


class _NationalStub(_StubSource):
    name = "national"
    confidence = Confidence.VERY_HIGH
    is_national_bibliography = True


class TestExactMatchEnforcement(unittest.TestCase):
    def test_rejects_candidate_with_different_isbn(self):
        source = _StubSource([Candidate({TRADE}, {"title": "Dante"})])
        self.assertIsNone(source.lookup(LIMITED))

    def test_accepts_exact_match(self):
        source = _StubSource([Candidate({LIMITED}, {"title": "Dante"})])
        result = source.lookup(LIMITED)
        self.assertIsNotNone(result)
        self.assertEqual(result.data["title"], "Dante")

    def test_accepts_isbn10_form_of_same_edition(self):
        source = _StubSource([Candidate({"1784961485"}, {"title": "Dante"})])
        self.assertIsNotNone(source.lookup(LIMITED))


class TestResolverStatuses(unittest.TestCase):
    def _resolver(self, sources):
        resolver = Resolver.__new__(Resolver)
        resolver.fetcher = None
        resolver.raw_dir = None
        resolver.sources = sources
        return resolver

    def test_invalid_isbn(self):
        record = self._resolver([]).resolve("123")
        self.assertEqual(record.status, Status.INVALID_ISBN)

    def test_absence_proven_when_national_source_answers(self):
        resolver = self._resolver([_NationalStub([])])
        record = resolver.resolve(LIMITED)
        self.assertEqual(record.status, Status.NOT_IN_NATIONAL_BIBLIOGRAPHY)

    def test_absence_not_proven_when_national_source_fails(self):
        resolver = self._resolver([_NationalStub([], error=FetchError("boom"))])
        record = resolver.resolve(LIMITED)
        self.assertEqual(record.status, Status.UNRESOLVED)

    def test_secondary_failure_does_not_block_absence_verdict(self):
        """A blocked retailer must not stop us proving library absence."""
        resolver = self._resolver(
            [_NationalStub([]), _StubSource([], error=FetchError("403"))]
        )
        record = resolver.resolve(LIMITED)
        self.assertEqual(record.status, Status.NOT_IN_NATIONAL_BIBLIOGRAPHY)

    def test_near_miss_edition_does_not_resolve(self):
        """A record for another Dante edition must never satisfy the query."""
        resolver = self._resolver([_NationalStub([Candidate({TRADE}, {"title": "Dante"})])])
        record = resolver.resolve(LIMITED)
        self.assertEqual(record.status, Status.NOT_IN_NATIONAL_BIBLIOGRAPHY)
        self.assertEqual(record.title, "")

    def test_special_edition_flagged_from_catalogue_evidence(self):
        resolver = self._resolver(
            [_NationalStub([Candidate({LIMITED}, {"edition_statement": "Limited edition, signed"})])]
        )
        record = resolver.resolve(LIMITED)
        self.assertEqual(record.status, Status.RESOLVED)
        self.assertTrue(record.special_contents)
        self.assertTrue(record.signed.startswith("yes"))


class TestMerge(unittest.TestCase):
    def test_higher_confidence_wins_single_valued_field(self):
        strong = SourceRecord("nls", Confidence.VERY_HIGH, {"title": "Dante"})
        weak = SourceRecord("googlebooks", Confidence.MEDIUM, {"title": "DANTE (WH40K)"})
        merged = merge(LIMITED, [weak, strong], "now")
        self.assertEqual(merged.title, "Dante")
        self.assertEqual(merged.field_provenance["title"], "nls")
        self.assertEqual(merged.confidence, "VERY_HIGH")

    def test_weaker_source_fills_gaps(self):
        strong = SourceRecord("nls", Confidence.VERY_HIGH, {"title": "Dante"})
        weak = SourceRecord("openlibrary", Confidence.MEDIUM, {"cover_url": "http://x/y.jpg"})
        merged = merge(LIMITED, [strong, weak], "now")
        self.assertEqual(merged.cover_url, "http://x/y.jpg")
        self.assertEqual(merged.field_provenance["cover_url"], "openlibrary")

    def test_multi_valued_fields_accumulate_without_duplicates(self):
        a = SourceRecord("nls", Confidence.VERY_HIGH, {"subjects": ["Science fiction."]})
        b = SourceRecord("openlibrary", Confidence.MEDIUM, {"subjects": ["Science fiction.", "Fiction"]})
        merged = merge(LIMITED, [a, b], "now")
        self.assertEqual(merged.subjects, ["Science fiction.", "Fiction"])

    def test_isbn_always_from_query_not_source(self):
        rogue = SourceRecord("bad", Confidence.VERY_HIGH, {"isbn": TRADE})
        merged = merge(LIMITED, [rogue], "now")
        self.assertEqual(merged.isbn, LIMITED)


class TestCsv(unittest.TestCase):
    def test_writes_header_and_flattens_lists(self):
        record = merge(
            LIMITED,
            [SourceRecord("nls", Confidence.VERY_HIGH, {"title": "Dante", "subjects": ["A", "B"]})],
            "now",
        )
        buffer = io.StringIO()
        count = write_csv([record], buffer)
        lines = buffer.getvalue().splitlines()
        self.assertEqual(count, 1)
        self.assertIn("isbn,isbn10,title", lines[0])
        self.assertIn("A | B", lines[1])


if __name__ == "__main__":
    unittest.main()
