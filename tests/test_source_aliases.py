import unittest

from qb_agent.quickbooks import _SOURCE_ALIASES, _rep_key


def resolved(written: str, settings: dict[str, str] | None = None) -> str:
    """How a Lead Source spelling is read, built the way the gateway builds it."""
    table = {
        **_SOURCE_ALIASES,
        **{_rep_key(said): meant for said, meant in (settings or {}).items()},
    }
    return table.get(_rep_key(written), written)


class SourceAliasTests(unittest.TestCase):
    """Lead Source names a person, and the sheet writes that person several
    ways. Each unrecognised spelling adds another entry to the QuickBooks Rep
    list, so one colleague ends up with two and her invoices split between
    them."""

    def test_every_spelling_of_tej_reaches_one_person(self):
        """The sheet writes "Tej V" 44 times and plain "Tej" 13."""
        for written in ("Tej V", "Tej", "tej", "TEJ V", "Tej V."):
            self.assertEqual(resolved(written), "TEJAL VAIDYA", written)

    def test_the_others_the_office_has_already_told_us(self):
        self.assertEqual(resolved("Tahir"), "Taher")
        self.assertEqual(resolved("tahir"), "Taher")
        self.assertEqual(resolved("Amber"), "AMBREEN MERCHANT")
        self.assertEqual(resolved("Razia winback"), "Razia")

    def test_a_name_nobody_has_mapped_is_left_alone(self):
        """Most Lead Sources are simply the person's name, and a new colleague
        must still become a rep rather than being bent onto an existing one."""
        for written in ("Nizar", "Zainab Zakauddin", "Cross Sell"):
            self.assertEqual(resolved(written), written)

    def test_the_settings_can_still_override_a_line(self):
        """Another company file may have a different Tej. config.json wins."""
        self.assertEqual(
            resolved("Tej V", {"Tej V": "SOMEBODY ELSE"}), "SOMEBODY ELSE"
        )

    def test_the_settings_can_still_add_a_line(self):
        self.assertEqual(resolved("Shanu", {"Shanu": "Shahnaz Sutar"}), "Shahnaz Sutar")

    def test_the_table_is_keyed_the_way_it_is_looked_up(self):
        """A key with capitals or a full stop would never be found, because the
        lookup goes through _rep_key first."""
        for key in _SOURCE_ALIASES:
            self.assertEqual(key, _rep_key(key), f"{key!r} is not in _rep_key form")


if __name__ == "__main__":
    unittest.main()
