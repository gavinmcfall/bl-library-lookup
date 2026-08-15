import unittest

from blmeta.wayback import extract, slugify

# Condensed from real archived blacklibrary.com pages (2015-2018 layouts).
PAGE_2016 = """
<html><body>
<h1>The Realmgate Wars: War Storm (Exclusive Edition)</h1>
<p>Fewer than 250 in stock</p>
<div>Exclusive Edition: <span>$ 40.00</span></div>
<b>ABOUT THIS EDITION</b><br>
<p>Beautifully presented with bespoke artwork on an embossed, soft-touch matte
finish hardcover, brass-edged pages and metallic inking, this 304-page tome
contains the stories 'Borne by the Storm' by Nick Kyme.</p>
<h2>More to Explore</h2>
</body></html>
"""

PAGE_2018 = """
<html><body>
<h1>Primarchs: Corax (Limited Edition)</h1>
<p>Only 2,500 copies available worldwide</p>
<p>Each copy is individually numbered and limited to 2,500 copies.</p>
<b>ABOUT THIS EDITION</b>
<p>$ 65.00</p>
<p>A luxurious numbered hardback with soft-touch cover, foiled page edges and
a ribbon marker, presented in a printed slipcase with an exclusive short
story.</p>
</body></html>
"""


class TestExtract(unittest.TestCase):
    def test_2016_layout(self):
        details = extract(PAGE_2016)
        self.assertEqual(
            details["product_title"], "The Realmgate Wars: War Storm (Exclusive Edition)"
        )
        self.assertIn("Fewer than 250 in stock", details["availability"])
        self.assertIn("brass-edged pages", details["about_edition"])
        self.assertIn("$40.00", details["prices_seen"])

    def test_2018_layout_skips_price_paragraph(self):
        """The first <p> after ABOUT THIS EDITION is a price on this layout;
        the description is the first paragraph with substance."""
        details = extract(PAGE_2018)
        self.assertIn("slipcase", details["about_edition"])
        self.assertNotEqual(details["about_edition"], "$ 65.00")

    def test_print_run_and_numbering(self):
        details = extract(PAGE_2018)
        self.assertEqual(details["print_run"], "2500")
        self.assertTrue(details["numbered"])

    def test_no_false_signed_flag(self):
        self.assertNotIn("mentions_signed", extract(PAGE_2016))


class TestSlugify(unittest.TestCase):
    def test_spaces_and_case(self):
        self.assertEqual(slugify("Magnus the Red"), "magnus-the-red")

    def test_punctuation(self):
        self.assertEqual(slugify("Mephiston: Lord of Death"), "mephiston-lord-of-death")


if __name__ == "__main__":
    unittest.main()
