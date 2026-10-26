import unittest
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from qb_agent.models import WorkRecord
from qb_agent.processor import Processor
from qb_agent.quickbooks import CustomerEntity


def row(**extra) -> WorkRecord:
    return WorkRecord(
        source_id="row-1",
        row_number=2,
        customer_name="TETSING LLC 02",
        job_name="Commercial Property",
        invoice_number="POL-1",
        invoice_date=date(2026, 10, 2),
        amount=Decimal("450"),
        extra=tuple(extra.items()),
    )


@dataclass
class FakeGateway:
    """A QuickBooks holding one customer with one oddly-spelt job."""

    customers: dict[str, CustomerEntity] = field(default_factory=dict)
    jobs_asked_for: int = 0

    def find_customer_by_id(self, list_id: str) -> CustomerEntity | None:
        return self.customers.get(list_id)

    def find_customer(self, full_name: str) -> CustomerEntity | None:
        return next(
            (one for one in self.customers.values() if one.full_name == full_name), None
        )

    def customer_jobs(self, customer) -> list[CustomerEntity]:
        self.jobs_asked_for += 1
        return [
            one for one in self.customers.values()
            if one.full_name.startswith(f"{customer.full_name}:")
        ]


def find(gateway, customer, lob, known=""):
    """The job lookup alone. It reads nothing off the processor it hangs from."""
    return Processor._find_lob_job(None, gateway, customer, lob, known)


class SavedJobIdTests(unittest.TestCase):
    """The line of business is written a dozen ways between the sheet and
    QuickBooks, so finding the job by name is a judgement made afresh on every
    send. Once the job has been reached, the judgement is settled: the ID says
    which job it is, whatever it is called today."""

    def setUp(self):
        self.customer = CustomerEntity("cust-1", "TETSING LLC 02", "1", "", "")
        self.job = CustomerEntity("job-1", "TETSING LLC 02:Prpty", "1", "", "cust-1")
        self.gateway = FakeGateway(customers={"cust-1": self.customer, "job-1": self.job})

    def test_a_saved_id_finds_the_job_without_reading_names(self):
        found, matches = find(self.gateway, self.customer, "Commercial Property", "job-1")
        self.assertEqual(found.list_id, "job-1")
        self.assertEqual(matches, [found])
        self.assertEqual(self.gateway.jobs_asked_for, 0)   # nothing was compared

    def test_without_an_id_the_name_is_still_matched(self):
        """The spelling table stays. It is what gets a row to its job the
        first time, before any ID has been written down."""
        found, _ = find(self.gateway, self.customer, "Commercial Property")
        self.assertEqual(found.list_id, "job-1")
        self.assertEqual(self.gateway.jobs_asked_for, 1)

    def test_a_deleted_job_falls_back_to_the_name(self):
        """An ID for a job somebody has since deleted must not strand the row.
        It is a shortcut, not the only way in."""
        found, _ = find(self.gateway, self.customer, "Commercial Property", "job-gone")
        self.assertEqual(found.list_id, "job-1")

    def test_a_job_under_a_different_customer_is_refused(self):
        """Pointing one customer's row at another customer's job is the exact
        mistake the ID exists to prevent, so a stale or mistyped ID is checked
        against the customer before it is believed."""
        other = CustomerEntity("job-2", "SOMEBODY ELSE LLC:Prpty", "1", "", "cust-2")
        self.gateway.customers["job-2"] = other
        found, _ = find(self.gateway, self.customer, "Commercial Property", "job-2")
        self.assertEqual(found.list_id, "job-1")

    def test_a_renamed_job_is_still_found_by_its_id(self):
        """The point of the whole thing: somebody tidies the job's name in
        QuickBooks, and the row still goes to the same job."""
        self.gateway.customers["job-1"] = CustomerEntity(
            "job-1", "TETSING LLC 02:Commercial Prop (2026)", "1", "", "cust-1"
        )
        found, _ = find(self.gateway, self.customer, "Commercial Property", "job-1")
        self.assertEqual(found.list_id, "job-1")


class RowReadsTheSavedIdTests(unittest.TestCase):
    """The ID reaches the row from the sheet's hidden column."""

    def test_the_column_is_read_off_the_row(self):
        self.assertEqual(row(qb_job_id="job-1").mapped_job_list_id, "job-1")

    def test_a_row_never_sent_has_none(self):
        self.assertEqual(row().mapped_job_list_id, "")


if __name__ == "__main__":
    unittest.main()
