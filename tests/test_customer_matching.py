import unittest

from qb_agent.quickbooks import (
    CustomerEntity,
    _base_company_name,
    _rank_customers,
    _same_company,
)


def names(entities):
    return [entity.full_name for entity in entities]


def customers(*full_names):
    return [CustomerEntity(f"id-{n}", name) for n, name in enumerate(full_names)]


class BaseCompanyNameTests(unittest.TestCase):
    """A business trades from several addresses, and QuickBooks holds one
    customer per address. What they share is the company name up to its LLC,
    Inc or the like - so that is what the picker gathers them by."""

    def test_the_name_is_cut_at_its_legal_suffix(self):
        for written, expected in [
            ("Dpeg Retail LLC 1903 Hamilton Rd", "dpeg retail llc"),
            ("DPEG RETAIL LLC LOC 1903", "dpeg retail llc"),
            ("DPEG Reh Holdings, LLC- Sharyland Registered Series", "dpeg reh holdings llc"),
            ("Atlantic Express Savannah Corp", "atlantic express savannah corp"),
            ("Satellite Blvd 4601, LLC DBA Stop", "satellite blvd 4601 llc"),
        ]:
            self.assertEqual(_base_company_name(written), expected, written)

    def test_a_name_without_one_gathers_nothing(self):
        """Nothing says where the company ends, and guessing would sweep in
        every business whose name happens to start the same way."""
        self.assertEqual(_base_company_name("Henderson Hospitality"), "")

    def test_the_first_word_is_never_the_suffix(self):
        """A business actually called "Co-op Market" must not reduce to "co"
        and match half the customer list."""
        self.assertEqual(_base_company_name("Co-op Market"), "")

    def test_a_longer_name_is_not_the_same_company(self):
        self.assertTrue(_same_company("dpeg retail llc", "DPEG RETAIL LLC LOC 1903"))
        self.assertTrue(_same_company("dpeg retail llc", "Dpeg Retail LLC"))
        self.assertFalse(_same_company("dpeg retail llc", "Dpeg Retailers LLC"))
        self.assertFalse(_same_company("", "Anything At All"))


class RankCustomersTests(unittest.TestCase):
    def test_every_account_under_the_company_is_offered(self):
        """However the location is spelt. The sheet writes an address and
        QuickBooks writes a LOC number; the two resemble each other no more
        than a coincidence would, so resemblance cannot be what decides it."""
        ranked = _rank_customers(
            "Dpeg Retail LLC 1903 Hamilton Rd",
            customers(
                "Dpeg Retail LLC",
                "DPEG RETAIL LLC LOC 1903",
                "DPEG RETAIL LLC LOC 110 HWY 6",
                "Completely Unrelated Inc",
            ),
        )
        self.assertEqual(
            sorted(names(ranked)),
            ["DPEG RETAIL LLC LOC 110 HWY 6", "DPEG RETAIL LLC LOC 1903", "Dpeg Retail LLC"],
        )

    def test_the_plain_company_is_offered_beside_its_locations(self):
        """It is shorter than the name being matched, which scores it low -
        but it is often the right answer, and leaving it out forces a second
        customer to be created for a company already there."""
        ranked = _rank_customers(
            "Dpeg Retail LLC 1903 Hamilton Rd", customers("Dpeg Retail LLC")
        )
        self.assertEqual(names(ranked), ["Dpeg Retail LLC"])

    def test_another_company_is_still_refused(self):
        ranked = _rank_customers(
            "Dpeg Retail LLC 1903 Hamilton Rd",
            customers("Henderson Hospitality LLC", "Monroe Fuel Ventures LLC"),
        )
        self.assertEqual(names(ranked), [])

    def test_a_name_with_no_suffix_still_matches_on_resemblance(self):
        ranked = _rank_customers(
            "Henderson Hospitality Cascades",
            customers("HENDERSON HOSPITALITY CASCADES MOUNTAIN RESORT", "Monroe Fuel"),
        )
        self.assertEqual(names(ranked), ["HENDERSON HOSPITALITY CASCADES MOUNTAIN RESORT"])


if __name__ == "__main__":
    unittest.main()
