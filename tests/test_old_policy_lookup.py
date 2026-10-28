import unittest
from dataclasses import dataclass, field

from qb_agent.quickbooks import CustomerEntity, InvoiceEntity
from qb_agent.runtime import Runtime, _names_agree


@dataclass
class FakeRecord:
    """Only the three things the lookup reads."""

    source_tab: str = "CL Renewal Pipeline"
    old_policy_number: str = ""


@dataclass
class FakeGateway:
    """A QuickBooks that holds a few invoices and the customers behind them."""

    invoices: dict[str, list[InvoiceEntity]] = field(default_factory=dict)
    customers: dict[str, CustomerEntity] = field(default_factory=dict)

    def find_invoices(self, ref_number: str) -> list[InvoiceEntity]:
        return self.invoices.get(ref_number, [])

    def find_customer_by_id(self, list_id: str) -> CustomerEntity | None:
        return self.customers.get(list_id)

    def find_customer(self, full_name: str) -> CustomerEntity | None:
        return next(
            (one for one in self.customers.values() if one.full_name == full_name), None
        )


def look_up(gateway, record):
    """The lookup alone. It reads nothing off the runtime it hangs from."""
    return Runtime._old_policy_customers(None, gateway, record)


class OldPolicyLookupTests(unittest.TestCase):
    """A renewal's expiring policy number is the strongest link back to the
    customer it belongs to: whoever was billed for last year's policy is who
    this year's belongs to. Everything else about a renewal row - the
    corporation name, the location - is a guess beside it."""

    def setUp(self):
        self.parent = CustomerEntity("cust-1", "ALBERT FOOD MART LLC", "1", "", "")
        self.job = CustomerEntity(
            "job-1", "ALBERT FOOD MART LLC:Commercial Property", "1", "", "cust-1"
        )
        self.gateway = FakeGateway(customers={"cust-1": self.parent, "job-1": self.job})

    def bill(self, policy: str, to: str, memo: str = "") -> None:
        self.gateway.invoices.setdefault(policy, []).append(
            InvoiceEntity(f"txn-{policy}-{to}", policy, to, "1", "", memo)
        )

    def test_the_customer_billed_last_year_is_found(self):
        self.bill("WS668444", to="cust-1")
        found, exact = look_up(self.gateway, FakeRecord(old_policy_number="WS668444"))
        self.assertEqual([one.list_id for one in found], ["cust-1"])
        self.assertTrue(exact)

    def test_an_invoice_on_a_job_resolves_to_the_customer_above_it(self):
        """Invoices are raised against the job, not the customer. The picker
        offers customers, so the job has to be climbed out of first."""
        self.bill("WS668444", to="job-1")
        found, _ = look_up(self.gateway, FakeRecord(old_policy_number="WS668444"))
        self.assertEqual([one.full_name for one in found], ["ALBERT FOOD MART LLC"])

    def test_two_customers_billed_for_one_policy_are_both_offered(self):
        other = CustomerEntity("cust-2", "ALBERT FOOD MART GA LLC", "1", "", "")
        self.gateway.customers["cust-2"] = other
        self.bill("WS668444", to="cust-1")
        self.bill("WS668444", to="cust-2")
        found, _ = look_up(self.gateway, FakeRecord(old_policy_number="WS668444"))
        self.assertEqual(len(found), 2)   # a question for a person, not a guess

    def test_a_number_nobody_was_billed_for_finds_nothing(self):
        found, _ = look_up(self.gateway, FakeRecord(old_policy_number="WS000000"))
        self.assertEqual(found, [])

    def test_new_business_is_never_looked_up_this_way(self):
        """New Business rows have no expiring policy, and reading one from a
        neighbouring column would bill a renewal to the wrong customer."""
        self.bill("WS668444", to="cust-1")
        found, _ = look_up(
            self.gateway,
            FakeRecord(source_tab="CL NB Pipeline", old_policy_number="WS668444"),
        )
        self.assertEqual(found, [])

    def test_a_shortened_number_is_matched_but_marked_unconfirmed(self):
        """Older invoices carry an 11-character number with the full one in the
        Memo. The customer is still found, but the caller is told it could not
        be proved, so the dashboard asks rather than deciding."""
        self.bill("CFP4802613-", to="cust-1", memo="Policy #: SOMETHING-ELSE")
        found, exact = look_up(
            self.gateway, FakeRecord(old_policy_number="CFP4802613-2025")
        )
        self.assertEqual([one.list_id for one in found], ["cust-1"])
        self.assertFalse(exact)

    def test_a_shortened_number_the_memo_confirms_is_exact(self):
        self.bill("CFP4802613-", to="cust-1", memo="Policy #: CFP4802613-2025")
        found, exact = look_up(
            self.gateway, FakeRecord(old_policy_number="CFP4802613-2025")
        )
        self.assertEqual([one.list_id for one in found], ["cust-1"])
        self.assertTrue(exact)


class NameSanityCheckTests(unittest.TestCase):
    """The expiring policy number decides the customer on its own, which is
    right until the number is wrong. A number mistyped into the sheet, or one
    a carrier has reused, points at a business with no connection to the row -
    and the renewal is then billed to a stranger without anyone being asked."""

    def test_the_same_business_written_differently_still_agrees(self):
        for written in (
            "ALBERT FOOD MART LLC",
            "Albert Food Mart, LLC",
            "ALBERT FOOD MART GA LLC",
            "ALBERT FOOD MART LLC:Commercial Property",
        ):
            self.assertTrue(_names_agree("ALBERT FOOD MART LLC", written), written)

    def test_an_unrelated_business_does_not_agree(self):
        self.assertFalse(_names_agree("TETSING LLC 02", "Sky Sigman,LLC"))

    def test_llc_alone_is_not_agreement(self):
        """Nearly every customer carries LLC or Inc. Counting it as a match
        would wave through exactly the case this check exists to catch."""
        self.assertFalse(_names_agree("ACME LLC", "ZENITH LLC"))

    def test_a_blank_name_agrees_with_nothing(self):
        self.assertFalse(_names_agree("", "ALBERT FOOD MART LLC"))


if __name__ == "__main__":
    unittest.main()
