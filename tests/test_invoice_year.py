import unittest
from dataclasses import dataclass, field
from datetime import date

from qb_agent.config import InvoiceKindConfig
from qb_agent.quickbooks import _invoice_year


@dataclass
class FakeTransport:
    """Answers an invoice query with whatever QuickBooks is pretended to hold."""

    invoices: list[dict] = field(default_factory=list)

    def request(self, qbxml: str) -> str:
        return "<fake/>"


class InvoiceYearTests(unittest.TestCase):
    """Some accounts keep one policy number from one year to the next. Looking
    an invoice up by that number alone finds last year's as readily as this
    year's, and the renewal then reopened the 2025 invoice and overwrote it
    instead of raising 2026's. The effective date is what tells them apart."""

    def test_the_year_is_read_off_a_quickbooks_date(self):
        self.assertEqual(_invoice_year("2025-09-01"), 2025)
        self.assertEqual(_invoice_year("2026-01-15"), 2026)

    def test_a_date_that_cannot_be_read_judges_nothing(self):
        """Zero means "do not decide on this". A guessed year would either
        overwrite last year's invoice or raise a duplicate beside it."""
        for unreadable in ("", "   ", "not a date", "20-09-2025", None):
            self.assertEqual(_invoice_year(unreadable), 0)


class FindPolicyInvoiceByYearTests(unittest.TestCase):
    """The lookup itself, with QuickBooks' answer supplied by hand."""

    def gateway(self, matches):
        from qb_agent.quickbooks import DesktopQuickBooksGateway

        gateway = DesktopQuickBooksGateway.__new__(DesktopQuickBooksGateway)
        gateway.transport = FakeTransport()
        gateway.builder = _Builder()
        gateway.premium = InvoiceKindConfig(
            template="", ar_account="Accounts Receivable", item=""
        )
        gateway.commission = gateway.premium
        gateway.fees = gateway.premium
        gateway._raise_on_error = lambda xml: None
        gateway._matches = matches
        import qb_agent.quickbooks as module

        self._original = module.parse_invoice_matches
        module.parse_invoice_matches = lambda xml: matches
        self.addCleanup(setattr, module, "parse_invoice_matches", self._original)
        return gateway

    def invoice(self, txn_id, ref, txn_date, job="job-1"):
        return {
            "txn_id": txn_id,
            "ref_number": ref,
            "edit_sequence": "1",
            "customer_list_id": job,
            "ar_account": "Accounts Receivable",
            "line_id": "LINE-1",
            "memo": "",
            "txn_date": txn_date,
        }

    def test_last_years_invoice_is_not_this_years(self):
        """The bug as reported: 2025's invoice answered a 2026 renewal, and
        sending it rewrote the 2025 figures."""
        gateway = self.gateway([self.invoice("txn-2025", "POL-1", "2025-09-01")])
        found = gateway.find_policy_invoice("POL-1", "premium", "job-1", policy_year=2026)
        self.assertIsNone(found, "a 2026 renewal must raise its own invoice")

    def test_this_years_invoice_is_found_and_reused(self):
        """Sending the same row twice must correct the invoice, not duplicate it."""
        gateway = self.gateway([self.invoice("txn-2026", "POL-1", "2026-09-01")])
        found = gateway.find_policy_invoice("POL-1", "premium", "job-1", policy_year=2026)
        self.assertIsNotNone(found)
        self.assertEqual(found.txn_id, "txn-2026")

    def test_the_right_year_is_picked_out_of_several(self):
        gateway = self.gateway([
            self.invoice("txn-2024", "POL-1", "2024-09-01"),
            self.invoice("txn-2025", "POL-1", "2025-09-01"),
            self.invoice("txn-2026", "POL-1", "2026-09-01"),
        ])
        found = gateway.find_policy_invoice("POL-1", "premium", "job-1", policy_year=2025)
        self.assertEqual(found.txn_id, "txn-2025")

    def test_without_an_effective_date_nothing_is_narrowed(self):
        """A row with no date behaves as it did before the year was looked at,
        rather than silently raising a second invoice every time."""
        gateway = self.gateway([self.invoice("txn-2025", "POL-1", "2025-09-01")])
        found = gateway.find_policy_invoice("POL-1", "premium", "job-1", policy_year=0)
        self.assertEqual(found.txn_id, "txn-2025")

    def test_an_invoice_with_no_date_is_still_a_match(self):
        """QuickBooks always sets a date, but an invoice that comes back
        without one must not be skipped into a duplicate."""
        gateway = self.gateway([self.invoice("txn-old", "POL-1", "")])
        found = gateway.find_policy_invoice("POL-1", "premium", "job-1", policy_year=2026)
        self.assertEqual(found.txn_id, "txn-old")


class _Builder:
    def invoice_query(self, ref_number, request_id="invoice-query"):
        return "<fake/>"

    def invoice_query_for_job(self, job_list_id, request_id="invoice-job"):
        return "<fake/>"


class DryRunYearTests(unittest.TestCase):
    """A dry run has to show what a real send would do, or it is worse than
    nothing: it would report "will update" for an invoice that is about to be
    created, and the mistake would only surface against live data."""

    def record(self, year):
        from qb_agent.models import WorkRecord

        return WorkRecord(
            source_id=f"row-{year}",
            row_number=year,
            customer_name="Acme Roofing LLC",
            invoice_number="POL-1",
            invoice_date=date(year, 9, 1),
        )

    def test_a_renewal_under_the_same_number_creates_its_own_invoice(self):
        from qb_agent.quickbooks import DryRunQuickBooksGateway

        gateway = DryRunQuickBooksGateway("Test Company")
        first, _ = gateway.create_policy_invoice(self.record(2025), "job-1", "premium")

        self.assertIsNotNone(
            gateway.find_policy_invoice("POL-1", "premium", "job-1", policy_year=2025)
        )
        self.assertIsNone(
            gateway.find_policy_invoice("POL-1", "premium", "job-1", policy_year=2026),
            "2026 must not find the 2025 invoice",
        )

    def test_the_renewal_finds_last_years_customer(self):
        """Narrowing by year must not reach the renewal lookup, which exists
        to find whoever was billed for the expiring policy."""
        from qb_agent.quickbooks import DryRunQuickBooksGateway

        gateway = DryRunQuickBooksGateway("Test Company")
        gateway.create_policy_invoice(self.record(2025), "job-1", "premium")
        self.assertEqual(len(gateway.find_invoices("POL-1")), 1)


if __name__ == "__main__":
    unittest.main()
