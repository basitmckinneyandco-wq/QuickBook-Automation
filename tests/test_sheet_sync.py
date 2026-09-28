import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from qb_agent.config import load_config
from qb_agent.runtime import Runtime, _policy_key
from qb_agent.websource import build_records
from tests.test_websource import ROWS


class StaleReadTests(unittest.TestCase):
    """The sheet watcher reads the sheet while a send may be writing to it.

    A read that began a moment before a write landed answers with the old
    word. Taken at face value, a premium just created - and just written as
    "Created" - was read back as the old "Pending", treated as somebody typing
    Pending, and this machine's record of it wiped. The badge came back a few
    seconds later with the mark that means "done on another computer".
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        (folder / "config.json").write_text(
            json.dumps(
                {
                    "mode": "quickbooks",
                    "database_path": "test.db",
                    "google_sheet": {
                        "key_file": "key.json",
                        "spreadsheet_id": "SHEET",
                        "read_rows": False,
                        "tabs": {"nb": "CL NB Pipeline"},
                    },
                }
            ),
            encoding="utf-8",
        )
        self.runtime = Runtime(load_config(folder / "config.json"))
        record = build_records("nb", ROWS).records[0]
        extra = dict(record.extra)
        extra["qb_premium"] = "Created"         # just written by this machine
        self.record = replace(record, extra=tuple(extra.items()))
        self.runtime.records = {self.record.source_id: self.record}
        self.runtime.store.save_invoice(
            self.record, "quickbooks", "premium",
            status="completed", message="Premium invoice created.", txn_id="TXN-1",
        )
        # The sheet, as the read saw it: still the old word.
        key = _policy_key(self.record)
        self.runtime._row_lookup = lambda label, force=False: {
            key: (5, {"qb_premium": "Pending"})
        }
        self.runtime._push_settled = lambda: 0

    def premium(self):
        held = self.runtime.store.invoice_states([self.record.source_id], "quickbooks")
        return held.get(self.record.source_id, {}).get("premium")

    def test_a_read_begun_before_our_write_does_not_wipe_the_invoice(self):
        # Our write finished after the read began.
        self.runtime._written_at[(self.record.source_id, "qb_premium")] = float("inf")
        self.runtime.refresh_statuses()
        self.assertIsNotNone(self.premium(), "the premium record was wiped")
        held = dict(self.runtime.records[self.record.source_id].extra)
        self.assertEqual(held["qb_premium"], "Created")

    def test_somebody_really_typing_pending_is_still_obeyed(self):
        # No write of ours anywhere near: this Pending is a person's.
        self.runtime.refresh_statuses()
        self.assertIsNone(self.premium(), "a genuine reset was ignored")


if __name__ == "__main__":
    unittest.main()
