import unittest

from qb_agent.processor import _canonical_lob, _lob_names_match


class LineOfBusinessTests(unittest.TestCase):
    """What the agency's own company file actually contains: 6,963 jobs under
    439 different spellings of about sixty covers. A spelling the matcher does
    not recognise creates a second job beside the real one, and the invoices
    then split across the two.

    The pairs below were read out of that file, so each one is a mistake that
    has already been made by hand at least once.
    """

    def assertSame(self, left, right):
        self.assertTrue(
            _lob_names_match(left, right),
            f"{left!r} and {right!r} should be one job, not two",
        )

    def assertDifferent(self, left, right):
        self.assertFalse(
            _lob_names_match(left, right),
            f"{left!r} and {right!r} are different covers and must stay apart",
        )

    def test_property_however_it_is_shortened(self):
        """The spelling that started this: QuickBooks holds Prpty, the sheet
        sends Commercial Property, and a second job appeared every time."""
        for spelling in ("Prpty", "Propty", "Property", "Prop", "PPTY", "Proprty",
                         "Proprety", "Comm Prpty", "Comm Propty", "Comp Prpty",
                         "COMMERICAL PROPERTY"):
            self.assertSame("Commercial Property", spelling)

    def test_vacant_property_keeps_its_own_shortenings(self):
        """Vacant Property is a separate cover, and its own misspellings have
        to reach it rather than falling through to Commercial Property."""
        self.assertSame("Vacant Property", "Vacant prpty")
        self.assertSame("Vacant Property", "Vacant propty")
        self.assertDifferent("Commercial Property", "Vacant Property")

    def test_a_one_word_name_still_matches_a_two_word_one(self):
        """The rule underneath compares word by word, so Prpty against
        Commercial Property never matched however the words were spelt."""
        self.assertSame("Commercial Property", "Prpty")
        self.assertSame("Liquor Liability", "LL")
        self.assertSame("Equipment Breakdown", "EB")
        self.assertSame("General Liability", "Genl")

    def test_liquor_liability_including_its_misspelling(self):
        for spelling in ("LL", "Liq", "Liquor", "Liquor Liab", "Liqour Liability",
                         "Liqour Liabilty"):
            self.assertSame("Liquor Liability", spelling)

    def test_commercial_and_personal_motor_are_never_run_together(self):
        """Different covers. Merging them would move invoices onto the wrong
        job, which is far harder to undo than a duplicate."""
        self.assertDifferent("Commercial Auto", "Personal Auto")
        self.assertDifferent("Auto", "Commercial Auto")
        self.assertSame("Commercial Auto", "Auto (CL)")
        self.assertSame("Commercial Auto", "Comm Auto")
        self.assertSame("Personal Auto", "Auto")       # plain Auto is the personal one
        self.assertSame("Personal Auto", "Auto(P)")
        self.assertSame("Personal Auto", "Auto (PRSNL)")

    def test_homeowners_keeps_its_bracketed_covers_apart(self):
        self.assertSame("Homeowners", "Home Owners")
        self.assertSame("Homeowners", "Home")
        self.assertSame("Homeowners", "Homeoners")
        self.assertDifferent("Homeowners", "Homeowners (Condo)")
        self.assertDifferent("Homeowners", "Homeowners (Renters)")
        self.assertDifferent("Homeowners", "Renters")

    def test_umbrella_absorbs_its_abbreviations_but_not_the_personal_one(self):
        for spelling in ("Umb", "Umberlla", "Umb-Comm", "Commercial Umb",
                         "Excess Liability"):
            self.assertSame("Umbrella", spelling)
        self.assertDifferent("Umbrella", "Umbrella - Personal")

    def test_a_numbered_job_is_its_own_policy(self):
        """Property 1 and Property 2 are two policies on one customer. Folding
        them into Property would put both years' invoices on one job."""
        self.assertDifferent("Property", "Property Layer 1")
        self.assertDifferent("Flood", "Flood 2")
        self.assertDifferent("Wind", "Wind 2")
        self.assertDifferent("BOP", "BOP & Umbrella")

    def test_misspellings_people_actually_typed(self):
        self.assertSame("Workers Compensation", "Wokrer Comp")
        self.assertSame("Workers Compensation", "Worker's Compensation")
        self.assertSame("Bond", "Surey Bond")
        self.assertSame("Package", "Pakage")
        self.assertSame("Equipment Breakdown", "Equipement Breakdown")
        self.assertSame("Builders Risk", "Builder's Risk")

    def test_firearms_cover_under_all_of_its_names(self):
        for spelling in ("Firearms", "Fire Arms", "Fire Arm", "Shooting",
                         "Shooting Liability", "Fire Arms Liability",
                         "Firm Arm Liability", "Assailant"):
            self.assertSame("Active Assailant", spelling)

    def test_unrelated_covers_are_still_unrelated(self):
        self.assertDifferent("General Liability", "Liquor Liability")
        self.assertDifferent("Bond", "Performance Bond")
        self.assertDifferent("Commercial Property", "Vacant Property")
        self.assertDifferent("Equipment Breakdown", "Equipment")
        self.assertDifferent("Workers Compensation", "Package")

    def test_every_spelling_of_one_cover_lands_on_one_name(self):
        """Whatever reaches QuickBooks, both sides reduce to the same word -
        which is what lets either spelling find the other."""
        self.assertEqual(_canonical_lob("Prpty"), _canonical_lob("Commercial Property"))
        self.assertEqual(_canonical_lob("LL"), _canonical_lob("Liquor Liability"))
        self.assertEqual(_canonical_lob("Auto"), _canonical_lob("Personal Auto"))


if __name__ == "__main__":
    unittest.main()
