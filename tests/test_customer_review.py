import unittest
from datetime import date
from decimal import Decimal

from qb_agent.models import WorkRecord


def row(**changes) -> WorkRecord:
    values = dict(
        source_id="row-1",
        row_number=2,
        customer_name="965 McEver LLC",
        job_name="Surety Bond",
        invoice_number="101661121",
        invoice_date=date(2026, 10, 10),
        item="CCI Surety Inc",
        amount=Decimal("105"),
    )
    values.update(changes)
    return WorkRecord(**values)


class CustomerStepReviewTests(unittest.TestCase):
    """The Customers/Jobs column answers for one step: creating the customer
    and the job. It was reporting every problem on the row, including ones
    that stop only an invoice - so a policy earning no commission was shown as
    a customer needing attention, and the person went looking at a customer
    that was perfectly fine."""

    def test_a_zero_amount_does_not_block_the_customer(self):
        """Creating a customer and a job needs no money at all. The amount is
        the invoice line, and the invoice badges answer for it."""
        self.assertEqual(row(amount=Decimal("0")).customer_reasons(), [])
        self.assertIn("Amount must be greater than zero.", row(amount=Decimal("0")).review_reasons())

    def test_a_missing_item_does_not_block_the_customer(self):
        self.assertEqual(row(item="").customer_reasons(), [])

    def test_a_missing_date_does_not_block_the_customer(self):
        self.assertEqual(row(invoice_date=None).customer_reasons(), [])

    def test_a_missing_name_does_block_it(self):
        reasons = row(customer_name="").customer_reasons()
        self.assertTrue(any(reason.startswith("Customer name") for reason in reasons))

    def test_a_missing_policy_number_does_block_it(self):
        """Not an invoice field here: without it there is nothing to tell this
        policy apart from the next one for the same customer, so the job it
        creates could be orphaned or duplicated."""
        reasons = row(invoice_number="").customer_reasons()
        self.assertTrue(any(reason.startswith("Policy number") for reason in reasons))

    def test_an_over_long_job_name_does_block_it(self):
        reasons = row(job_name="Surety Bond " * 20).customer_reasons()
        self.assertTrue(any(reason.startswith("Job name") for reason in reasons))


if __name__ == "__main__":
    unittest.main()
