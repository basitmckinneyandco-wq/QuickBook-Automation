import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from qb_agent.models import WorkRecord
from qb_agent.storage import Store


def row(billing: str, **extra) -> WorkRecord:
    return WorkRecord(
        source_id="row-1",
        row_number=2,
        customer_name="Acme Roofing LLC",
        invoice_number="POL-1",
        invoice_date=date(2026, 9, 1),
        amount=Decimal("450"),
        extra=(("billing", billing), *extra.items()),
    )


class DirectBillingTests(unittest.TestCase):
    """On a direct-billed policy the carrier bills the insured, so the agency
    raises no premium invoice. The office does this by hand on every such row;
    the dashboard now starts there instead, while leaving the decision theirs
    to reverse."""

    def test_direct_billing_is_recognised_however_it_is_written(self):
        for written in ("Direct Billing", "Direct Bill", "direct", "DIRECT BILLING"):
            self.assertTrue(row(written).direct_billed, written)

    def test_agency_billing_is_not_direct(self):
        for written in ("Agency Billing", "Agency", "", "Full Payment"):
            self.assertFalse(row(written).direct_billed, written)

    def test_a_direct_billed_policy_wants_no_premium_invoice(self):
        self.assertFalse(row("Direct Billing").invoice_applies("premium"))

    def test_an_agency_billed_policy_still_wants_one(self):
        self.assertTrue(row("Agency Billing").invoice_applies("premium"))

    def test_billing_decides_the_premium_and_nothing_else(self):
        """Commission and fees answer "is this wanted?" from their own amounts.
        Direct billing says nothing about either, and must not silence a
        commission the agency has earned."""
        direct = row("Direct Billing")
        self.assertTrue(direct.invoice_applies("commission"))    # this row earns one
        self.assertFalse(direct.invoice_applies("fees"))         # and carries no fee


class WantingItBackTests(unittest.TestCase):
    """Marking the premium "Need it after all" has to outrank the rule, or the
    office cannot raise the one direct-billed premium they do want. Clearing
    the mark was enough while every default was "wanted"; against a default of
    "not needed" there is nothing to clear, and the button did nothing."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = Store(str(Path(self.folder.name) / "history.db"))

    def held(self):
        states = self.store.invoice_states(["row-1"], "quickbooks")
        state = states.get("row-1", {}).get("premium")
        return state.status if state is not None else None

    def test_wanting_it_is_written_down_in_its_own_right(self):
        self.assertIsNone(self.held())
        self.store.set_invoice_needed("row-1", "quickbooks", "premium", True)
        self.assertEqual(self.held(), "ready")

    def test_not_wanting_it_still_marks_it(self):
        self.store.set_invoice_needed("row-1", "quickbooks", "premium", False)
        self.assertEqual(self.held(), "not_needed")

    def test_the_two_marks_replace_each_other(self):
        self.store.set_invoice_needed("row-1", "quickbooks", "premium", False)
        self.store.set_invoice_needed("row-1", "quickbooks", "premium", True)
        self.assertEqual(self.held(), "ready")
        self.store.set_invoice_needed("row-1", "quickbooks", "premium", False)
        self.assertEqual(self.held(), "not_needed")

    def test_a_recorded_failure_is_not_overwritten(self):
        """A failure is something that happened. Asking for the invoice again
        must not erase the reason the last attempt did not work."""
        self.store.save_invoice(
            row("Direct Billing"), "quickbooks", "premium",
            status="failed", message="Could not be raised",
            error_message="QuickBooks error 3140",
        )
        self.store.set_invoice_needed("row-1", "quickbooks", "premium", True)
        self.assertEqual(self.held(), "failed")

    def test_an_invoice_that_exists_cannot_be_called_unwanted(self):
        self.store.save_invoice(
            row("Direct Billing"), "quickbooks", "premium",
            status="completed", message="Raised", txn_id="TXN-1",
        )
        with self.assertRaises(ValueError):
            self.store.set_invoice_needed("row-1", "quickbooks", "premium", False)


if __name__ == "__main__":
    unittest.main()
