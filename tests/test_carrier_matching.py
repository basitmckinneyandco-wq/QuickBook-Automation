import unittest

from qb_agent.quickbooks import _carrier_key, _group_by_key, _CARRIER_ALIASES


# A slice of the agency's own Item List, including the two entries that
# shorten to the same thing.
ITEMS = [
    "American Management Corporation", "Hanover Insurance", "CNA Insurance",
    "Appalachian Underwriters Inc", "CrossCover Insurance", "JenCap Insurance",
    "Utica National Insurance", "Philadelphia Insurance", "CRC Atlanta",
    "Encova", "Amtrust", "RTS", "RPS", "Assurant Flood", "Employers Prefered Ins",
    "Burns & Wilcox", "Neptune Flood", "Foremost Insurance", "NationWide",
    "Vintage Underwriters", "Tokio marine", "STARFISH", "biBERK Business Insurance",
    "Bass Underwriter", "Bass Underwriters",
]


def match(written: str) -> str:
    """What _resolve_item would settle on, without a QuickBooks session."""
    by_name = {item.casefold(): item for item in ITEMS}
    if written.casefold() in by_name:
        return by_name[written.casefold()]
    alias = _CARRIER_ALIASES.get(written.casefold())
    if alias and alias.casefold() in by_name:
        return by_name[alias.casefold()]
    keys = _group_by_key(ITEMS)
    for key in (_carrier_key(written), _carrier_key(written).replace(" ", "")):
        found = keys.get(key, ())
        if len(found) == 1:
            return found[0]
    return ""          # a new item would be created


class CarrierMatchingTests(unittest.TestCase):
    """The sheet and the Item List rarely spell a carrier the same way. Taken
    literally, each difference made a second item for a carrier already there,
    and one carrier's commission then sat under two names.

    Every pair below was read out of the agency's own sheet and its own Item
    List, so each one is a duplicate that has already been made by hand.
    """

    def assertMatches(self, written: str, expected: str):
        self.assertEqual(match(written), expected, f"{written!r} should reach {expected!r}")

    def test_initials_reach_the_full_name(self):
        """The one that started this: the sheet says AMC all day."""
        for written in ("AMC", "amc", "american management", "American Managemant"):
            self.assertMatches(written, "American Management Corporation")

    def test_a_trailing_insurance_is_not_a_different_carrier(self):
        self.assertMatches("Hanover", "Hanover Insurance")
        self.assertMatches("CNA", "CNA Insurance")
        self.assertMatches("foremost", "Foremost Insurance")
        self.assertMatches("Nationwide Insurance", "NationWide")

    def test_punctuation_and_case_are_ignored(self):
        self.assertMatches("Appalachian Underwriters, Inc", "Appalachian Underwriters Inc")
        self.assertMatches("JENCAP", "JenCap Insurance")

    def test_spacing_is_ignored(self):
        """CrossCover and cross cover; STARFISH and star fish."""
        self.assertMatches("cross cover", "CrossCover Insurance")
        self.assertMatches("star fish", "STARFISH")
        self.assertMatches("TOKIOMARINE", "Tokio marine")

    def test_the_recurring_misspellings(self):
        self.assertMatches("Applachian", "Appalachian Underwriters Inc")
        self.assertMatches("traevelers", "")        # Travelers is not in this slice
        self.assertMatches("burns&wilcox", "Burns & Wilcox")

    def test_the_office_rulings(self):
        """Answers given by the people who do this work, not guesses."""
        self.assertMatches("Ryan Turner Specialty", "RTS")
        self.assertMatches("assurant", "Assurant Flood")
        for written in ("Employers", "employer", "Employers Ins. Co. of NV"):
            self.assertMatches(written, "Employers Prefered Ins")

    def test_two_items_that_shorten_alike_are_left_alone(self):
        """The Item List holds both Bass Underwriter and Bass Underwriters.
        Choosing between them would be a guess, and a guess puts commission on
        the wrong carrier - so a new item is made and a person can see it."""
        self.assertMatches("bass uw", "")
        self.assertMatches("bass", "")

    def test_an_exact_name_still_wins(self):
        self.assertMatches("Bass Underwriters", "Bass Underwriters")
        self.assertMatches("RTS", "RTS")

    def test_an_unknown_carrier_is_not_forced_onto_something(self):
        for written in ("Blitz", "PPIB", "SIU", "Umbrella Pro"):
            self.assertMatches(written, "")

    def test_an_alias_whose_item_is_absent_falls_through(self):
        """Another company file need not hold the same carriers. The alias
        names a real item; without it, nothing is invented."""
        self.assertEqual(_CARRIER_ALIASES["phly"], "Philadelphia Insurance")
        self.assertEqual(match("phly"), "Philadelphia Insurance")

    def test_filler_alone_never_becomes_the_key(self):
        """A carrier actually called "Insurance Group" must not shorten to
        nothing and then match everything else that shortens to nothing."""
        self.assertEqual(_carrier_key("The Insurance Company"), "the insurance company")
        self.assertNotEqual(_carrier_key("Hanover"), _carrier_key("The Insurance Company"))


if __name__ == "__main__":
    unittest.main()
