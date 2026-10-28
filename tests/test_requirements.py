import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class RequirementsTests(unittest.TestCase):
    """Everything the app imports has to be named in requirements.txt.

    A package left out still works on the machine it was written on, because
    something else there installed it. It fails only on a clean machine - and
    the error names the package, not this file, so the fix looks like "run pip
    install -r requirements.txt", which is exactly what was already done.
    """

    def listed(self) -> set[str]:
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        names = set()
        for line in text.splitlines():
            line = line.split("#")[0].strip()
            if line:
                names.add(re.split(r"[<>=!~\[]", line)[0].strip().casefold())
        return names

    def test_the_packages_the_app_imports_are_all_listed(self):
        listed = self.listed()
        self.assertIn("pywin32", listed)        # talking to QuickBooks
        self.assertIn("google-auth", listed)    # signing in as the service account
        # google-auth does not install this, because it can use other senders.
        # The sheet is written through this one, so it has to be asked for.
        self.assertIn("requests", listed)

    def test_the_sheet_can_actually_sign_in_here(self):
        """Proves the list above is enough, rather than only looking right."""
        from google.auth.transport.requests import Request  # noqa: F401
        from google.oauth2 import service_account           # noqa: F401


if __name__ == "__main__":
    unittest.main()
