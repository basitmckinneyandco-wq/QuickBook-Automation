import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from qb_agent.edits import EditError, apply, apply_all
from qb_agent.storage import Store
from qb_agent.websource import build_records
from tests.test_websource import ROWS


def first_record():
    return build_records("nb", ROWS).records[0]


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.record = first_record()

    def test_text_field_is_replaced(self):
        updated = apply(self.record, "customer_name", "SUNSHINE STORES INC")
        self.assertEqual(updated.customer_name, "SUNSHINE STORES INC")

    def test_long_customer_name_is_shortened_for_quickbooks_without_blocking_send(self):
        long_name = "X" * 60
        updated = apply(self.record, "customer_name", long_name)
        self.assertEqual(updated.review_reasons(), [])
        self.assertTrue(updated.customer_name_was_shortened)
        self.assertEqual(updated.quickbooks_customer_name, "X" * 41)

    def test_money_is_parsed_and_rounded(self):
        updated = apply(self.record, "amount", "$1,234.567")
        self.assertEqual(updated.amount, Decimal("1234.57"))

    def test_a_non_number_in_a_money_field_is_refused(self):
        with self.assertRaises(EditError):
            apply(self.record, "amount", "abc")

    def test_dates_accept_several_formats(self):
        self.assertEqual(apply(self.record, "invoice_date", "2026-03-04").invoice_date, date(2026, 3, 4))
        self.assertEqual(apply(self.record, "invoice_date", "03/04/2026").invoice_date, date(2026, 3, 4))

    def test_a_bad_date_is_refused(self):
        with self.assertRaises(EditError):
            apply(self.record, "invoice_date", "someday")

    def test_clearing_a_date_is_allowed(self):
        self.assertIsNone(apply(self.record, "invoice_date", "").invoice_date)

    def test_display_only_columns_can_be_edited_too(self):
        updated = apply(self.record, "net_due", "123.45")
        self.assertEqual(dict(updated.extra)["net_due"], "123.45")

    def test_an_unknown_column_is_refused(self):
        with self.assertRaises(EditError):
            apply(self.record, "not_a_column", "x")

    def test_editing_the_job_changes_the_full_name(self):
        updated = apply(self.record, "job_name", "Commercial Property")
        self.assertEqual(updated.job_full_name, f"{updated.customer_name}:Commercial Property")

    def test_apply_all_skips_values_that_no_longer_fit(self):
        updated = apply_all(self.record, {"customer_name": "Fixed Co", "amount": "nonsense"})
        self.assertEqual(updated.customer_name, "Fixed Co")
        self.assertEqual(updated.amount, self.record.amount)


class OverrideStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.db")

    def test_overrides_round_trip(self):
        self.store.set_override("ROW-1", "customer_name", "Fixed Co")
        self.assertEqual(self.store.overrides(), {"ROW-1": {"customer_name": "Fixed Co"}})

    def test_a_second_edit_replaces_the_first(self):
        self.store.set_override("ROW-1", "customer_name", "First")
        self.store.set_override("ROW-1", "customer_name", "Second")
        self.assertEqual(self.store.overrides()["ROW-1"]["customer_name"], "Second")

    def test_clearing_removes_it(self):
        self.store.set_override("ROW-1", "amount", "5")
        self.store.clear_override("ROW-1", "amount")
        self.assertEqual(self.store.overrides(), {})

    def test_edits_survive_a_fresh_download_of_the_sheet(self):
        record = first_record()
        corrected = "SUNSHINE SAN AUGUSTINE"
        self.assertNotEqual(record.customer_name, corrected)
        self.store.set_override(record.source_id, "customer_name", corrected)

        # Re-download: the rows come back exactly as the sheet has them...
        rebuilt = build_records("nb", ROWS).records[0]
        self.assertEqual(rebuilt.customer_name, record.customer_name)

        # ...but re-applying the stored edits restores the correction.
        saved = self.store.overrides().get(rebuilt.source_id, {})
        self.assertEqual(apply_all(rebuilt, saved).customer_name, corrected)


if __name__ == "__main__":
    unittest.main()
