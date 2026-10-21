import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qb_agent.quickbooks import _normal_path, _share_path, same_company_file


class SharedCompanyFileTests(unittest.TestCase):
    """One company file on a network share has a different name on every
    machine that reaches it. The settings file travels between those machines,
    so the name saved on one must still be recognised on the next - otherwise
    the app says the wrong company is open and refuses to do anything."""

    SHARE = r"\\alizain\d\Intuit\QuickBooks\Company Files\mighty ventures llc.qbw"
    MAPPED = r"Z:\Intuit\QuickBooks\Company Files\mighty ventures llc.qbw"

    def as_mapped(self, path):
        """Windows answering 'Z: is \\\\alizain\\d'.

        Anything else comes back unchanged, which is what the real one does
        when Windows says the drive is not a mapping.
        """
        if path.upper().startswith("Z:"):
            return self.SHARE.replace("mighty ventures llc.qbw", Path(path).name)
        return path

    def test_a_mapped_drive_matches_the_share_behind_it(self):
        with patch("qb_agent.quickbooks._share_path", self.as_mapped):
            self.assertTrue(same_company_file(self.MAPPED, self.SHARE))
            self.assertTrue(same_company_file(self.SHARE, self.MAPPED))

    def test_a_different_company_on_the_same_share_is_still_refused(self):
        other = self.SHARE.replace("mighty ventures llc", "TESTING LLC")
        with patch("qb_agent.quickbooks._share_path", self.as_mapped):
            self.assertFalse(same_company_file(self.MAPPED, other))

    def test_the_same_file_under_two_names_is_recognised(self):
        """D:\\... on the machine sharing it and \\\\server\\d\\... from another
        are one file. No amount of reading the text says so; the file system
        does."""
        with tempfile.TemporaryDirectory() as folder:
            real = Path(folder) / "company.qbw"
            real.write_text("x", encoding="utf-8")
            indirect = Path(folder) / "." / "company.qbw"
            self.assertTrue(same_company_file(str(real), str(indirect)))

    def test_a_path_that_cannot_be_reached_is_not_a_match(self):
        with tempfile.TemporaryDirectory() as folder:
            real = Path(folder) / "company.qbw"
            real.write_text("x", encoding="utf-8")
            missing = Path(folder) / "gone.qbw"
            self.assertFalse(same_company_file(str(real), str(missing)))

    def test_nothing_matches_an_empty_setting(self):
        self.assertFalse(same_company_file("", self.SHARE))
        self.assertFalse(same_company_file(self.SHARE, "   "))

    def test_a_local_path_is_left_alone(self):
        """Windows raises for a drive that is not mapped; the plain path is
        kept, because a guess would match nothing at all."""
        local = r"C:\Users\Public\Documents\Intuit\company.qbw"
        self.assertEqual(_share_path(local), local)
        self.assertEqual(_normal_path(local), os.path.normcase(os.path.abspath(local)))

    def test_a_share_path_is_left_alone(self):
        self.assertEqual(_share_path(self.SHARE), self.SHARE)


if __name__ == "__main__":
    unittest.main()
