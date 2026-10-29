import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from qb_agent.config import (
    AppConfig,
    CompanyConfig,
    DataSourceConfig,
    InvoiceKindConfig,
)
from qb_agent.importer import load_records
from qb_agent.models import Operation
from qb_agent.processor import Processor
from qb_agent.quickbooks import DryRunQuickBooksGateway, QuickBooksError
from qb_agent.runtime import Runtime
from qb_agent.storage import Store

CSV = (
    "Customer Name,Invoice Number,Invoice Date,Item,Amount\n"
    "Acme Roofing,INV-1,09/01/2026,Services,450\n"
    "Acme Roofing,INV-2,09/02/2026,Services,450\n"
    ",INV-3,09/03/2026,Services,450\n"
).encode("utf-8")


def make_config(**overrides) -> AppConfig:
    base = dict(
        mode="dry_run",
        allow_writes=False,
        listen_host="127.0.0.1",
        listen_port=8770,
        database_path=":memory:",
        app_name="Test App",
        qbxml_version="13.0",
        invoice_template="",
        premium=InvoiceKindConfig(
            template="Mckinney & Co.(Premium Payment)",
            ar_account="Accounts Receivable:Premium",
        ),
        commission=InvoiceKindConfig(
            template="Mckinney & Co. (Comm)",
            ar_account="Accounts Receivable:Commission",
            item_income_account="Commission Income",
        ),
        fees=InvoiceKindConfig(
            template="Mckinney & Co. (Comm)",
            ar_account="Accounts Receivable:Handling Fees",
            item="Fees",
            item_income_account="Commission Income",
        ),
        company=CompanyConfig(display_name="Testing LLC", file_path=""),
        data_source=DataSourceConfig(url="", default_tab=""),
    )
    base.update(overrides)
    return AppConfig(**base)


class ProcessorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.db")
        self.gateway = DryRunQuickBooksGateway("Testing LLC")
        self.records = load_records("book.csv", CSV).records

    def processor(self, config=None) -> Processor:
        return Processor(config or make_config(), self.store, lambda: self.gateway)

    def test_agency_bill_is_created_after_premium_is_marked_created_and_not_twice(self):
        record = replace(
            self.records[0],
            job_name="General Liability",
            extra=(
                ("billing", "Agency Bill"),
                ("net_due", "1250.50"),
                ("qb_customer_target", "Acme Roofing"),
                ("qb_customer_id", "CUSTOMER-1"),
                ("qb_customer_action", "match"),
                ("qb_job_target", "Acme Roofing:General Liability"),
                ("qb_job_id", "JOB-1"),
                ("qb_premium", "Created"),
            ),
        )
        first = self.processor().process_many([record], Operation.CREATE_BILL)[0]
        self.assertEqual(first.status, "completed")
        self.assertEqual(first.operation, "create_bill")
        saved = self.store.invoice_states([record.source_id], "dry_run")
        self.assertEqual(saved[record.source_id]["bill"].status, "completed")

        second = self.processor().process_many([record], Operation.CREATE_BILL)[0]
        self.assertEqual(second.status, "already_exists")

    def test_bill_is_blocked_until_premium_invoice_is_created(self):
        record = replace(
            self.records[0],
            extra=(
                ("billing", "Agency Bill"), ("net_due", "1250.50"),
                ("qb_customer_id", "CUSTOMER-1"), ("qb_customer_action", "match"),
                ("qb_job_id", "JOB-1"),
            ),
        )
        result = self.processor().process_many([record], Operation.CREATE_BILL)[0]
        self.assertEqual(result.status, "needs_review")
        self.assertIn("premium invoice", result.message.lower())

    def test_good_rows_complete_and_the_bad_row_is_held_back(self):
        results = self.processor().process_many(self.records)
        statuses = [result.status for result in results]
        self.assertEqual(statuses, ["completed", "completed", "needs_review"])
        self.assertIn("Customer name is missing.", results[2].message)

    def test_the_customer_is_created_once_and_reused(self):
        results = self.processor().process_many(self.records[:2])
        self.assertIn("created", results[0].steps[0])
        self.assertIn("reused", results[1].steps[0])
        self.assertEqual(results[0].customer_list_id, results[1].customer_list_id)

    def test_running_twice_does_not_create_the_invoice_twice(self):
        processor = self.processor()
        processor.process_many(self.records[:1])
        second = processor.process_many(self.records[:1])
        self.assertEqual(second[0].status, "already_exists")
        self.assertIn("already processed", second[0].message)

    def test_an_invoice_number_owned_by_another_customer_is_refused(self):
        processor = self.processor()
        processor.process_many(self.records[:1])
        clash = replace(self.records[0], source_id="ROW-OTHER", customer_name="Bluebird Cafe")
        result = processor.process_many([clash])[0]
        self.assertEqual(result.status, "failed")
        self.assertIn("different customer", result.message)

    def test_live_mode_without_allow_writes_is_refused(self):
        config = make_config(mode="quickbooks", allow_writes=False)
        result = self.processor(config).process_many(self.records[:1])[0]
        self.assertEqual(result.status, "failed")
        self.assertIn("allow_writes", result.message)

    def test_live_mode_without_a_company_file_is_refused(self):
        config = make_config(mode="quickbooks", allow_writes=True)
        result = self.processor(config).process_many(self.records[:1])[0]
        self.assertEqual(result.status, "failed")
        self.assertIn("company.file_path", result.message)

    def test_dry_run_results_are_marked_as_dry_run(self):
        result = self.processor().process_many(self.records[:1])[0]
        self.assertTrue(result.dry_run)


POLICY_CSV = (
    "Corporation Name,Line of Business,Carrier,Policy No#,EFF,Description,Quantity,Rate\n"
    "2 TESTING LLC,Commercial Property,Services,POL-1,09/01/2026,XYZ,1,500.00\n"
    "2 TESTING LLC,General Liability,Services,POL-2,09/01/2026,XYZ,1,325.00\n"
).encode("utf-8")


class PolicyJobTests(unittest.TestCase):
    """The insurance flow: corporation -> job per policy -> invoice on the job."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.db")
        self.gateway = DryRunQuickBooksGateway("Testing LLC")
        self.records = load_records("policies.csv", POLICY_CSV).records
        self.processor = Processor(make_config(), self.store, lambda: self.gateway)

    def test_columns_map_to_customer_job_and_item(self):
        record = self.records[0]
        self.assertEqual(record.customer_name, "2 TESTING LLC")
        self.assertEqual(record.job_name, "Commercial Property")
        self.assertEqual(record.job_full_name, "2 TESTING LLC:Commercial Property")
        self.assertEqual(record.item, "Services")
        self.assertEqual(record.description, "XYZ")
        self.assertEqual(record.invoice_number, "POL-1")

    def test_one_customer_two_jobs_two_invoices(self):
        results = self.processor.process_many(self.records)
        self.assertEqual([result.status for result in results], ["completed", "completed"])

        first, second = results
        self.assertIn("Customer '2 TESTING LLC' created.", first.steps)
        self.assertIn("Job '2 TESTING LLC:Commercial Property' created.", first.steps)
        # The second policy reuses the corporation but gets its own job.
        self.assertIn("reused", second.steps[0])
        self.assertIn("Job '2 TESTING LLC:General Liability' created.", second.steps)
        self.assertEqual(first.customer_list_id, second.customer_list_id)
        self.assertNotEqual(first.job_list_id, second.job_list_id)

    def test_the_invoice_is_raised_against_the_job_not_the_customer(self):
        result = self.processor.process_many(self.records[:1])[0]
        invoice = self.gateway.invoices["pol-1"]
        self.assertEqual(invoice.customer_list_id, result.job_list_id)
        self.assertNotEqual(invoice.customer_list_id, result.customer_list_id)

    def test_a_row_with_no_job_still_invoices_the_customer_directly(self):
        plain = load_records("book.csv", CSV).records[0]
        result = self.processor.process_many([plain])[0]
        self.assertEqual(result.status, "completed")

    def test_a_policy_missing_its_number_is_not_sent_as_a_customer_either(self):
        # A row with a Carrier/Line of Business but no Policy No# - like Summit
        # Utah Retail LLC in the real sheet - must not create a customer or job
        # even via the customer-only "Send" action, since the policy number is
        # what stops that job being duplicated once the real number arrives.
        no_policy = replace(self.records[0], invoice_number="")
        results = self.processor.process_many([no_policy], operation=Operation.CREATE_CUSTOMER)
        self.assertEqual(results[0].status, "needs_review")
        self.assertIn("Policy number is missing", results[0].message)
        self.assertEqual(self.gateway.customers, {})

    def test_a_policy_with_its_number_can_still_be_sent_as_a_customer_only(self):
        result = self.processor.process_many(
            [self.records[0]], operation=Operation.CREATE_CUSTOMER
        )[0]
        self.assertEqual(result.status, "completed")
        self.assertIsNotNone(result.job_list_id)   # the policy job is still created
        self.assertIsNone(result.invoice_txn_id)   # but no invoice - CREATE_CUSTOMER stops there

    def test_a_selected_location_is_reused_without_renaming_or_duplication(self):
        location = "Dpeg Retail LLC LOC 110 HWY 6"
        existing = self.gateway.create_customer(
            replace(self.records[0], customer_name=location, company_name=location)
        )
        record = replace(
            self.records[0],
            customer_name="Dpeg Retail LLC",
            company_name="Dpeg Retail LLC",
            extra=(("qb_customer_target", location), ("qb_customer_id", existing.list_id)),
        )

        result = self.processor.process_many([record], Operation.CREATE_CUSTOMER)[0]

        self.assertEqual(result.customer_list_id, existing.list_id)
        self.assertIn(location.casefold(), self.gateway.customers)
        self.assertNotIn("dpeg retail llc", self.gateway.customers)
        self.assertIn(f"{location}:Commercial Property".casefold(), self.gateway.customers)

    def test_update_cannot_rename_a_selected_location_customer(self):
        location = "Dpeg Retail LLC LOC 110 HWY 6"
        existing = self.gateway.create_customer(
            replace(self.records[0], customer_name=location, company_name=location)
        )
        record = replace(
            self.records[0],
            customer_name="Dpeg Retail LLC",
            company_name="Dpeg Retail LLC",
            extra=(("qb_customer_target", location), ("qb_customer_id", existing.list_id)),
        )

        result = self.processor.process_many([record], Operation.UPDATE_CUSTOMER)[0]

        self.assertEqual(result.status, "needs_review")
        self.assertIn("was not changed", result.message)
        self.assertIn(location.casefold(), self.gateway.customers)

    def test_renewal_old_policy_identifies_the_parent_location(self):
        location = "Dpeg Retail LLC LOC 110 HWY 6"
        old = replace(
            self.records[0], customer_name=location, company_name=location,
            invoice_number="OLD-100", source_tab="Renewal",
        )
        customer = self.gateway.create_customer(old)
        job = self.gateway.create_job(old, customer)
        self.gateway.create_policy_invoice(old, job.list_id, "premium")
        renewal = replace(
            self.records[0], customer_name="Dpeg Retail LLC", company_name="Dpeg Retail LLC",
            invoice_number="NEW-900", source_tab="Renewal",
            extra=(("old_policy_number", "OLD-100"),),
        )
        runtime = Runtime(make_config())

        matches, exact = runtime._old_policy_customers(self.gateway, renewal)

        self.assertTrue(exact)
        self.assertEqual([(item.list_id, item.full_name) for item in matches], [
            (customer.list_id, location)
        ])

    def test_changed_old_policy_invalidates_an_automatic_mapping(self):
        record = replace(
            self.records[0], source_tab="Renewal",
            extra=(
                ("old_policy_number", "OLD-200"),
                ("qb_customer_id", "CUSTOMER-1"),
                ("qb_customer_action", "old_policy:old100"),
            ),
        )

        self.assertEqual(record.mapped_customer_list_id, "")

    def test_full_long_old_policy_is_searched_before_the_shortened_fallback(self):
        location = "Serena Props LLC 14605"
        old_number = "CFP4802613-2025"
        old = replace(
            self.records[0], customer_name=location, company_name=location,
            invoice_number=old_number, source_tab="Renewal",
        )
        customer = self.gateway.create_customer(old)
        job = self.gateway.create_job(old, customer)
        self.gateway.create_policy_invoice(old, job.list_id, "premium")
        renewal = replace(
            self.records[0], customer_name="Serena Props LLC", company_name="Serena Props LLC",
            invoice_number="10090", source_tab="Renewal",
            extra=(("old_policy_number", old_number),),
        )
        runtime = Runtime(make_config())

        matches, exact = runtime._old_policy_customers(self.gateway, renewal)

        self.assertTrue(exact)
        self.assertEqual([item.full_name for item in matches], [location])

    def test_new_business_location_automatically_selects_matching_loc_customer(self):
        record = replace(
            self.records[0], customer_name="Dpeg Retail LLC", company_name="Dpeg Retail LLC",
            source_tab="New Business",
            extra=(("location", "Dpeg Retail LLC 1903 Hamilton Rd"),),
        )
        runtime = Runtime(make_config())
        matched = runtime._dry_gateway.create_customer(
            replace(record, customer_name="DPEG RETAIL LLC LOC 1903", company_name="DPEG RETAIL LLC LOC 1903")
        )
        runtime._dry_gateway.create_customer(
            replace(record, customer_name="DPEG RETAIL LLC LOC 1904", company_name="DPEG RETAIL LLC LOC 1904")
        )
        runtime.records[record.source_id] = record

        resolution = runtime.customer_resolutions([record.source_id])

        self.assertEqual(resolution["ambiguous"], [])
        self.assertEqual(resolution["automatic_choices"], [{
            "source_id": record.source_id,
            "action": "new_business_location",
            "list_id": matched.list_id,
        }])

    def test_new_business_location_never_auto_selects_the_bare_customer(self):
        record = replace(
            self.records[0], customer_name="Dpeg Retail LLC", company_name="Dpeg Retail LLC",
            source_tab="New Business",
            extra=(("location", "Dpeg Retail LLC 1903 Hamilton Rd"),),
        )
        runtime = Runtime(make_config())
        runtime._dry_gateway.create_customer(record)
        runtime.records[record.source_id] = record

        resolution = runtime.customer_resolutions([record.source_id])

        self.assertEqual(resolution["automatic_choices"], [])
        self.assertEqual(len(resolution["ambiguous"]), 1)

    def test_dashboard_lob_reuses_unique_abbreviated_job_under_selected_customer(self):
        location = "Serena Props LLC 14605"
        existing = replace(
            self.records[0], customer_name=location, company_name=location,
            job_name="comm prpty", invoice_number="OLD-100", source_tab="Renewal",
        )
        customer = self.gateway.create_customer(existing)
        old_job = self.gateway.create_job(existing, customer)
        renewal = replace(
            self.records[0], customer_name="Serena Props LLC", company_name="Serena Props LLC",
            job_name="Commercial Property", invoice_number="NEW-100", source_tab="Renewal",
            extra=(
                ("old_policy_number", "OLD-100"),
                ("qb_customer_id", customer.list_id),
                ("qb_customer_target", location),
                ("qb_customer_action", "old_policy:old100"),
            ),
        )

        result = self.processor.process_many([renewal], Operation.CREATE_CUSTOMER)[0]

        self.assertEqual(result.job_list_id, old_job.list_id)
        self.assertIn("matches dashboard Line of Business", " ".join(result.steps))
        self.assertNotIn(f"{location}:commercial property".casefold(), self.gateway.customers)

    def test_general_liability_reuses_genl_liab_job(self):
        location = "Example Customer LOC 1"
        abbreviated = replace(
            self.records[0], customer_name=location, company_name=location,
            job_name="genl liab",
        )
        customer = self.gateway.create_customer(abbreviated)
        old_job = self.gateway.create_job(abbreviated, customer)
        record = replace(
            self.records[0], customer_name="Example Customer", company_name="Example Customer",
            job_name="General Liability",
            extra=(("qb_customer_id", customer.list_id), ("qb_customer_target", location)),
        )

        result = self.processor.process_many([record], Operation.CREATE_CUSTOMER)[0]

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.job_list_id, old_job.list_id)

    def test_business_lob_equivalents_reuse_the_existing_quickbooks_job(self):
        cases = [
            ("BOP", "Package"),
            ("Umbrella", "Excess Liability"),
            ("Umbrella - Comm", "Umbrella"),
            ("Firms Arms", "Active Assualat"),
            ("Shooting", "Firearms"),
            ("EPLI", "Accedental Medical Coverage"),
            ("Worker's Compensation", "Worker Comp"),
            ("Surety Bond", "Bond"),
            ("CGL", "Commercial General Liability"),
            ("D&O", "Directors and Officers"),
        ]
        for index, (sheet_lob, quickbooks_lob) in enumerate(cases):
            with self.subTest(sheet_lob=sheet_lob, quickbooks_lob=quickbooks_lob):
                location = f"LOB Test {sheet_lob}"
                existing = replace(
                    self.records[0], customer_name=location, company_name=location,
                    job_name=quickbooks_lob, source_id=f"LOB-EXISTING-{index}",
                )
                customer = self.gateway.create_customer(existing)
                job = self.gateway.create_job(existing, customer)
                record = replace(
                    self.records[0], customer_name="LOB Test", company_name="LOB Test",
                    job_name=sheet_lob, source_id=f"LOB-ROW-{index}",
                    extra=(("qb_customer_id", customer.list_id), ("qb_customer_target", location)),
                )

                result = self.processor.process_many([record], Operation.CREATE_CUSTOMER)[0]

                self.assertEqual(result.status, "completed")
                self.assertEqual(result.job_list_id, job.list_id)
                self.assertNotIn(
                    f"{location}:{sheet_lob}".casefold(), self.gateway.customers
                )

    def test_live_batch_reopens_after_eight_rows_to_give_quickbooks_a_break(self):
        config = make_config(
            mode="quickbooks",
            allow_writes=True,
            company=CompanyConfig(display_name="Testing LLC", file_path="C:\\testing.qbw"),
        )
        processor = Processor(config, self.store, lambda: self.gateway)
        records = [
            replace(
                self.records[0],
                source_id=f"PACE-{index}",
                invoice_number=f"PACE-{index}",
                extra=(("qb_customer_action", "create"),),
            )
            for index in range(9)
        ]

        with patch("qb_agent.processor.sleep") as paused:
            results = processor.process_many(records, Operation.CREATE_CUSTOMER)

        self.assertEqual([item.status for item in results], ["completed"] * 9)
        paused.assert_any_call(4.0)

    def test_unrelated_old_policy_job_is_not_reused(self):
        location = "Serena Props LLC 3504"
        old = replace(
            self.records[0], customer_name=location, company_name=location, job_name="package",
        )
        customer = self.gateway.create_customer(old)
        package_job = self.gateway.create_job(old, customer)
        record = replace(
            self.records[0], customer_name="Serena Props LLC", company_name="Serena Props LLC",
            job_name="Commercial Property",
            extra=(
                ("old_policy_number", "CMP4808373-2025"),
                ("qb_customer_id", customer.list_id),
                ("qb_customer_target", location),
                ("qb_customer_action", "old_policy:cmp48083732025"),
            ),
        )

        result = self.processor.process_many([record], Operation.CREATE_CUSTOMER)[0]

        self.assertEqual(result.status, "completed")
        self.assertNotEqual(result.job_list_id, package_job.list_id)
        self.assertIn(f"{location}:commercial property".casefold(), self.gateway.customers)

    def test_ambiguous_abbreviated_jobs_stop_for_review_without_creating(self):
        location = "Example Customer LOC 2"
        first = replace(
            self.records[0], customer_name=location, company_name=location, job_name="comm prpty",
        )
        second = replace(first, job_name="comm property")
        customer = self.gateway.create_customer(first)
        self.gateway.create_job(first, customer)
        self.gateway.create_job(second, customer)
        record = replace(
            self.records[0], customer_name="Example Customer", company_name="Example Customer",
            job_name="Commercial Property",
            extra=(("qb_customer_id", customer.list_id), ("qb_customer_target", location)),
        )

        result = self.processor.process_many([record], Operation.CREATE_CUSTOMER)[0]

        self.assertEqual(result.status, "needs_review")
        self.assertIn("More than one QuickBooks job matches", result.message)
        self.assertNotIn(f"{location}:commercial property".casefold(), self.gateway.customers)


class PremiumInvoiceTests(unittest.TestCase):
    """The premium invoice is raised on the job and tracked apart from it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.db")
        self.gateway = DryRunQuickBooksGateway("Testing LLC")
        self.processor = Processor(make_config(), self.store, lambda: self.gateway)
        base = load_records("policies.csv", POLICY_CSV).records[0]
        self.record = replace(
            base,
            extra=(
                ("carrier_type", "Standard"),
                ("policy_class", "Full Payment"),
                ("net_due", "750.00"),
                ("policy_type", "New Buiz"),
                ("lead_source", "Referral"),
            ),
        )

    def run_premium(self):
        return self.processor.process_many([self.record], Operation.PREMIUM_INVOICE)[0]

    def test_it_creates_the_customer_the_job_and_then_the_invoice(self):
        result = self.run_premium()
        self.assertEqual(result.status, "completed")
        self.assertIn("Customer '2 TESTING LLC' created.", result.steps)
        self.assertIn("Job '2 TESTING LLC:Commercial Property' created.", result.steps)
        self.assertTrue(any("Premium invoice" in step for step in result.steps))

    def test_running_twice_updates_rather_than_duplicating(self):
        first = self.run_premium()
        self.assertTrue(any("created" in step for step in first.steps))
        second = self.run_premium()
        self.assertEqual(second.status, "completed")
        self.assertEqual(second.message, "Premium invoice updated.")
        # One invoice, not two - the second run reused the same transaction.
        self.assertEqual(len(self.gateway.invoices), 1)
        self.assertEqual(second.invoice_txn_id, first.invoice_txn_id)

    def invoice_state(self, kind="premium"):
        held = self.store.invoice_states([self.record.source_id], "dry_run")
        return held.get(self.record.source_id, {}).get(kind)

    def test_the_invoice_status_is_kept_apart_from_the_customer_status(self):
        self.run_premium()
        self.assertEqual(self.invoice_state().status, "completed")
        # The customer and job were created on the way, so they read as done too.
        state = self.store.get(self.record.source_id, "dry_run")
        self.assertEqual(state.status, "completed")
        self.assertTrue(state.customer_list_id)
        self.assertTrue(state.job_list_id)

    def test_a_failed_invoice_does_not_mark_the_customer_failed(self):
        self.record = replace(self.record, extra=(("carrier_type", "Standard"),))
        self.run_premium()
        self.assertEqual(self.invoice_state().status, "needs_review")
        self.assertIsNone(self.store.get(self.record.source_id, "dry_run"))

    def test_premium_and_commission_are_two_invoices_not_one(self):
        """Both carry the same policy number, so only the kind tells them apart."""
        self.record = replace(
            self.record, item="Employers", amount=Decimal("108.00"), description="Comm Inv"
        )
        premium = self.run_premium()
        commission = self.processor.process_many(
            [self.record], Operation.COMMISSION_INVOICE
        )[0]
        self.assertEqual(commission.status, "completed")
        self.assertEqual(commission.message, "Commission invoice created.")
        self.assertNotEqual(commission.invoice_txn_id, premium.invoice_txn_id)
        self.assertEqual(len(self.gateway.invoices), 2)
        # And each keeps its own status.
        self.assertEqual(self.invoice_state("premium").status, "completed")
        self.assertEqual(self.invoice_state("commission").status, "completed")

    def test_a_fees_invoice_bills_the_mckinney_fee_on_the_fees_item(self):
        self.record = replace(
            self.record,
            extra=self.record.extra
            + (("mckinney_fee", "314"), ("fees_invoice", "Agency Fees:314")),
        )
        result = self.processor.process_many([self.record], Operation.FEES_INVOICE)[0]
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.message, "Fees invoice created.")
        self.assertTrue(any("item 'Fees'" in step for step in result.steps))
        self.assertTrue(any("314.00" in step for step in result.steps))

    def test_a_row_with_no_fee_never_raises_a_fees_invoice(self):
        result = self.processor.process_many([self.record], Operation.FEES_INVOICE)[0]
        self.assertEqual(result.status, "needs_review")
        self.assertIn("McKinney Fee must be greater than zero", result.message)

    def test_a_commission_invoice_needs_a_carrier_and_an_amount(self):
        self.record = replace(self.record, item="", amount=Decimal("0"))
        result = self.processor.process_many([self.record], Operation.COMMISSION_INVOICE)[0]
        self.assertEqual(result.status, "needs_review")
        self.assertIn("Carrier is empty", result.message)
        self.assertIn("Commission must be greater than zero", result.message)

    def test_a_row_missing_its_class_never_reaches_quickbooks(self):
        self.record = replace(
            self.record, extra=(("carrier_type", "Standard"), ("net_due", "750.00"))
        )
        result = self.run_premium()
        self.assertEqual(result.status, "needs_review")
        self.assertIn("Class is empty", result.message)
        self.assertEqual(self.gateway.invoices, {})


class UpdateJobTests(unittest.TestCase):
    """Editing the Line of Business must reach QuickBooks, not just the customer."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.db")
        self.gateway = DryRunQuickBooksGateway("Testing LLC")
        self.record = load_records("policies.csv", POLICY_CSV).records[0]
        self.processor = Processor(make_config(), self.store, lambda: self.gateway)
        # Create it first, the way Send would.
        self.processor.process_many([self.record], operation=Operation.CREATE_CUSTOMER)

    def job_names(self):
        return sorted(name for name in self.gateway.customers if ":" in name)

    def test_the_job_is_renamed_in_place_not_duplicated(self):
        self.assertEqual(self.job_names(), ["2 testing llc:commercial property"])

        renamed = replace(self.record, job_name="Liquor")
        result = self.processor.process_many(
            [renamed], operation=Operation.UPDATE_CUSTOMER
        )[0]

        self.assertEqual(result.status, "completed")
        self.assertTrue(any("renamed" in step for step in result.steps), result.steps)
        # The old job name is gone and there is exactly one job, not two.
        self.assertEqual(self.job_names(), ["2 testing llc:liquor"])

    def test_the_renamed_job_keeps_its_quickbooks_id(self):
        before = self.store.get(self.record.source_id, "dry_run").job_list_id
        renamed = replace(self.record, job_name="Liquor")
        result = self.processor.process_many(
            [renamed], operation=Operation.UPDATE_CUSTOMER
        )[0]
        # Same ListID means invoices already raised against the job survive.
        self.assertEqual(result.job_list_id, before)

    def test_an_unchanged_job_is_left_alone(self):
        result = self.processor.process_many(
            [self.record], operation=Operation.UPDATE_CUSTOMER
        )[0]
        self.assertTrue(any("already matches" in step for step in result.steps), result.steps)
        self.assertEqual(self.job_names(), ["2 testing llc:commercial property"])


class FlakySessionGateway(DryRunQuickBooksGateway):
    """Drops its session on the Nth request, the way QuickBooks sometimes does."""

    def __init__(self, fail_on: int):
        super().__init__("Testing LLC")
        self.fail_on = fail_on
        self.calls = 0

    def find_customer(self, full_name):
        self.calls += 1
        if self.calls == self.fail_on:
            raise QuickBooksError("QuickBooks rejected the request: The ticket parameter is invalid.")
        return super().find_customer(full_name)


class SessionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.db")
        self.records = load_records("book.csv", CSV).records[:2]

    def test_a_dropped_session_is_reopened_and_the_row_retried(self):
        gateway = FlakySessionGateway(fail_on=2)
        processor = Processor(make_config(), self.store, lambda: gateway)
        results = processor.process_many(self.records)
        self.assertEqual([result.status for result in results], ["completed", "completed"])

    def test_a_session_that_never_recovers_gives_up_and_reports(self):
        gateway = FlakySessionGateway(fail_on=1)
        gateway.fail_on = -1  # fail every call
        gateway.find_customer = lambda name: (_ for _ in ()).throw(
            QuickBooksError("The ticket parameter is invalid.")
        )
        processor = Processor(make_config(), self.store, lambda: gateway)
        results = processor.process_many(self.records[:1])
        self.assertEqual(results[0].status, "failed")
        self.assertIn("ticket", results[0].message.lower())


class ConfigSafetyTests(unittest.TestCase):
    def test_writes_need_every_switch(self):
        self.assertFalse(make_config().live_writes_enabled)
        self.assertFalse(make_config(mode="quickbooks").live_writes_enabled)
        self.assertFalse(make_config(mode="quickbooks", allow_writes=True).live_writes_enabled)
        unlocked = make_config(
            mode="quickbooks",
            allow_writes=True,
            company=CompanyConfig("Testing LLC", r"C:\QB\Testing.QBW"),
        )
        self.assertTrue(unlocked.live_writes_enabled)
        self.assertIsNone(unlocked.blocked_reason())


if __name__ == "__main__":
    unittest.main()
