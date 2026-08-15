import unittest

from blmeta import isbn


class TestValidation(unittest.TestCase):
    def test_valid_isbn13(self):
        self.assertTrue(isbn.is_valid_isbn13("9781784961480"))
        self.assertTrue(isbn.is_valid_isbn13("978-1-78496-148-0"))

    def test_invalid_isbn13_checksum(self):
        self.assertFalse(isbn.is_valid_isbn13("9781784961481"))

    def test_valid_isbn10_with_x(self):
        self.assertTrue(isbn.is_valid_isbn10("043942089X"))

    def test_rejects_wrong_length(self):
        self.assertFalse(isbn.is_valid_isbn13("97817849614"))
        self.assertFalse(isbn.is_valid_isbn10("12345"))

    def test_canonical_rejects_junk(self):
        self.assertIsNone(isbn.canonical("not-an-isbn"))
        self.assertIsNone(isbn.canonical(""))


class TestConversion(unittest.TestCase):
    def test_round_trip(self):
        thirteen = "9781784961480"
        ten = isbn.to_isbn10(thirteen)
        self.assertEqual(ten, "1784961485")
        self.assertEqual(isbn.to_isbn13(ten), thirteen)

    def test_no_isbn10_outside_978(self):
        # 979-prefixed ISBNs have no ISBN-10 equivalent.
        self.assertIsNone(isbn.to_isbn10("9791234567896"))


class TestExactMatching(unittest.TestCase):
    """The core principle: only the same edition may ever match."""

    def test_same_edition_across_forms(self):
        self.assertTrue(isbn.matches("9781784961480", "1784961485"))

    def test_different_dante_editions_never_match(self):
        limited = "9781784961480"
        for other in ("9781784965297", "9781784966669"):
            self.assertFalse(
                isbn.matches(limited, other),
                f"{other} must not match the limited edition",
            )

    def test_invalid_never_matches(self):
        self.assertFalse(isbn.matches("9781784961480", "garbage"))


class TestExtraction(unittest.TestCase):
    def test_strips_marc_qualifiers(self):
        self.assertEqual(
            isbn.extract("9781784965297 (hbk.) : £18.00"), "9781784965297"
        )

    def test_handles_hyphenated(self):
        self.assertEqual(isbn.extract("978-1-78496-529-7 (pbk.)"), "9781784965297")

    def test_rejects_price_only(self):
        self.assertIsNone(isbn.extract("£18.00"))

    def test_binding_hint(self):
        self.assertEqual(isbn.binding_hint("9781784965297 (hbk.)"), "hardback")
        self.assertEqual(isbn.binding_hint("(pbk.)"), "paperback")
        self.assertIsNone(isbn.binding_hint("(no clue)"))


if __name__ == "__main__":
    unittest.main()
