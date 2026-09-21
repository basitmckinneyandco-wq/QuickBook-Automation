import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from xml.etree import ElementTree as ET

from qb_agent.models import InvoiceSpec, WorkRecord
from qb_agent.qbxml import QbxmlBuilder, parse_response

BUILDER = QbxmlBuilder("13.0")

RECORD = WorkRecord(
    source_id="ROW-1",
    row_number=2,
    customer_name="Acme Roofing",
    company_name="Acme Roofing LLC",
    email="billing@acme.test",
    phone="555-0101",
    invoice_number="INV-1001",
    invoice_date=date(2026, 9, 1),
    item="Services",
    description="Monthly bookkeeping",
    quantity=Decimal("1"),
    rate=Decimal("0"),
    amount=Decimal("450"),
)


class QbxmlTests(unittest.TestCase):
    def test_customer_add_has_the_schema_order(self):
        xml = BUILDER.customer_add(RECORD)
        add = ET.fromstring(_body(xml)).find(".//CustomerAdd")
        self.assertEqual([child.tag for child in add], ["Name", "CompanyName", "Phone", "Email"])
        self.assertEqual(add.findtext("Name"), "Acme Roofing")

    def test_long_customer_name_is_shortened_and_full_name_is_saved_in_notes(self):
        full_name = "SUNSHINE STORES INC DBA SUNSHINE SAN AUGUSTINE"
        record = replace(RECORD, customer_name=full_name)
        add = ET.fromstring(_body(BUILDER.customer_add(record))).find(".//CustomerAdd")
        self.assertEqual(add.findtext("Name"), full_name[:41])
        self.assertEqual(add.findtext("Notes"), full_name)

    def test_line_breaks_in_a_name_never_reach_quickbooks(self):
        """Sheet cells often hold an Alt+Enter line break.

        A QuickBooks list name containing a newline or tab is a name nobody
        can search for or match against later, so whitespace is collapsed
        before the name is sent.
        """
        record = replace(
            RECORD,
            customer_name="TD & UY HONG PHAT LLC DBA \nPARK LAKE CLEANERS",
            job_name="General\tLiability",
            company_name="Top Shelf Mart LLC \r\nDBA: Top Shelf",
        )
        add = ET.fromstring(_body(BUILDER.customer_add(record))).find(".//CustomerAdd")
        for field in ("Name", "CompanyName"):
            value = add.findtext(field) or ""
            self.assertNotIn("\n", value)
            self.assertNotIn("\r", value)
            self.assertNotIn("\t", value)
        self.assertEqual(add.findtext("Name"), "TD & UY HONG PHAT LLC DBA PARK LAKE CLEAN")

        job = ET.fromstring(_body(BUILDER.job_add(record, "P-1"))).find(".//CustomerAdd")
        self.assertEqual(job.findtext("Name"), "General Liability")
        # The name used to find the job again must match what was written.
        self.assertTrue(record.quickbooks_job_full_name.endswith(":General Liability"))

        # The full original is still what goes into Notes, unchanged.
        self.assertIn("PARK LAKE CLEANERS", add.findtext("Notes"))

    def test_a_colon_in_a_name_becomes_a_dash(self):
        """QuickBooks reserves the colon to separate a customer from its job,
        so a name containing one is refused outright - and plenty of
        corporation names carry "DBA:"."""
        record = replace(
            RECORD,
            customer_name="TX Mardi Gras Daiquiri Inc. DBA: Mardi Gras",
            job_name="Liquor: Liability",
        )
        self.assertEqual(
            record.quickbooks_customer_name,
            "TX Mardi Gras Daiquiri Inc. DBA - Mardi G",   # 41 chars, QB's limit
        )
        self.assertEqual(len(record.quickbooks_customer_name), 41)
        self.assertNotIn(":", record.quickbooks_customer_name)
        self.assertEqual(record.quickbooks_job_name, "Liquor - Liability")

        # The job's full name still has exactly one colon: the one QuickBooks
        # itself puts between customer and job.
        self.assertEqual(record.quickbooks_job_full_name.count(":"), 1)

        # And the original is preserved where it can be read back.
        add = ET.fromstring(_body(BUILDER.customer_add(record))).find(".//CustomerAdd")
        self.assertIn("DBA: Mardi Gras", add.findtext("Notes"))

    def test_customer_update_saves_the_full_name_in_notes_too(self):
        full_name = "SUNSHINE STORES INC DBA SUNSHINE SAN AUGUSTINE"
        record = replace(RECORD, customer_name=full_name)
        change = ET.fromstring(_body(BUILDER.customer_mod(record, "L1", "E1"))).find(".//CustomerMod")
        self.assertEqual(change.findtext("Name"), full_name[:41])
        self.assertEqual(change.findtext("Notes"), full_name)

    def test_a_long_company_name_is_shortened_too_not_just_the_customer_name(self):
        # This is the real bug: company_name defaults to the same long value as
        # customer_name, and QuickBooks enforces the 41-char limit on BOTH fields.
        full_name = "SUNSHINE STORES INC DBA SUNSHINE SAN AUGUSTINE"
        record = replace(RECORD, customer_name=full_name, company_name=full_name)
        add = ET.fromstring(_body(BUILDER.customer_add(record))).find(".//CustomerAdd")
        self.assertEqual(add.findtext("CompanyName"), full_name[:41])
        self.assertLessEqual(len(add.findtext("CompanyName")), 41)

    def test_a_long_company_name_alone_still_triggers_notes(self):
        # Even when the customer name itself is short, a long CompanyName must
        # still be shortened and still needs the Notes fallback.
        record = replace(RECORD, customer_name="Short Co", company_name="X" * 60)
        add = ET.fromstring(_body(BUILDER.customer_add(record))).find(".//CustomerAdd")
        self.assertEqual(add.findtext("Name"), "Short Co")
        self.assertEqual(len(add.findtext("CompanyName")), 41)
        self.assertEqual(add.findtext("Notes"), "Short Co")

    def test_a_long_policy_number_is_shortened_for_refnumber(self):
        long_policy = "04-TOP-000070841"  # 16 chars, over the 11-char RefNumber limit
        record = replace(RECORD, invoice_number=long_policy)
        add = ET.fromstring(_body(BUILDER.invoice_add(record, "L1"))).find(".//InvoiceAdd")
        self.assertEqual(add.findtext("RefNumber"), long_policy[:11])
        self.assertLessEqual(len(add.findtext("RefNumber")), 11)
        self.assertEqual(add.findtext("Memo"), f"Policy #: {long_policy}")

    def test_a_short_policy_number_needs_no_memo(self):
        add = ET.fromstring(_body(BUILDER.invoice_add(RECORD, "L1"))).find(".//InvoiceAdd")
        self.assertEqual(add.findtext("RefNumber"), "INV-1001")
        self.assertIsNone(add.find("Memo"))

    def test_invoice_add_uses_amount_when_there_is_no_rate(self):
        xml = BUILDER.invoice_add(RECORD, "80000001-1234567890")
        add = ET.fromstring(_body(xml)).find(".//InvoiceAdd")
        self.assertEqual(add.findtext("CustomerRef/ListID"), "80000001-1234567890")
        self.assertEqual(add.findtext("TxnDate"), "2026-09-01")
        self.assertEqual(add.findtext("RefNumber"), "INV-1001")
        line = add.find("InvoiceLineAdd")
        self.assertEqual(line.findtext("ItemRef/FullName"), "Services")
        self.assertEqual(line.findtext("Amount"), "450.00")
        self.assertIsNone(line.find("Rate"))

    def test_invoice_add_uses_quantity_and_rate_when_given(self):
        record = replace(RECORD, quantity=Decimal("2"), rate=Decimal("125"), amount=Decimal("0"))
        line = ET.fromstring(_body(BUILDER.invoice_add(record, "L1"))).find(".//InvoiceLineAdd")
        self.assertEqual(line.findtext("Quantity"), "2")
        self.assertEqual(line.findtext("Rate"), "125.00")
        self.assertIsNone(line.find("Amount"))

    def test_template_is_only_sent_when_configured(self):
        without = ET.fromstring(_body(BUILDER.invoice_add(RECORD, "L1")))
        self.assertIsNone(without.find(".//TemplateRef"))
        with_template = ET.fromstring(_body(BUILDER.invoice_add(RECORD, "L1", "Intuit Professional Invoice")))
        self.assertEqual(with_template.findtext(".//TemplateRef/FullName"), "Intuit Professional Invoice")

    def test_version_header_is_declared(self):
        self.assertIn('<?qbxml version="13.0"?>', BUILDER.customer_query("Acme"))

    def test_parse_success_response(self):
        xml = """<?xml version="1.0"?><QBXML><QBXMLMsgsRs>
        <CustomerAddRs statusCode="0" statusSeverity="Info" statusMessage="Status OK">
          <CustomerRet><ListID>80000001-1</ListID><FullName>Acme Roofing</FullName></CustomerRet>
        </CustomerAddRs></QBXMLMsgsRs></QBXML>"""
        response = parse_response(xml)[0]
        self.assertTrue(response.ok)
        self.assertEqual(response.list_id, "80000001-1")
        self.assertEqual(response.full_name, "Acme Roofing")

    def test_not_found_is_recognised(self):
        xml = """<?xml version="1.0"?><QBXML><QBXMLMsgsRs>
        <CustomerQueryRs statusCode="500" statusSeverity="Info"
          statusMessage="Object 'Acme' specified in the request could not be found in QuickBooks."/>
        </QBXMLMsgsRs></QBXML>"""
        response = parse_response(xml)[0]
        self.assertFalse(response.ok)
        self.assertTrue(response.not_found)

    def test_real_error_is_not_mistaken_for_not_found(self):
        xml = """<?xml version="1.0"?><QBXML><QBXMLMsgsRs>
        <InvoiceAddRs statusCode="3140" statusSeverity="Error"
          statusMessage="There is an invalid reference to QuickBooks Item."/>
        </QBXMLMsgsRs></QBXML>"""
        response = parse_response(xml)[0]
        self.assertFalse(response.ok)
        self.assertFalse(response.not_found)


PREMIUM = replace(
    RECORD,
    customer_name="Zipmart 4271 LLC",
    job_name="General Liability",
    invoice_number="GL-2026-000123",
    description="Comm Inv 5566",
    extra=(
        ("carrier_type", "Non Standard"),
        ("policy_class", "Full Payment"),
        ("net_due", "1,234.50"),
        ("prem_inv", "Premium Payment: 1234.5 & Commission: 160.55 & Policy Type: Renewal"),
        ("policy_type", "Renewal"),
        ("lead_source", "Referral"),
    ),
)


def premium_spec(record=PREMIUM, **overrides):
    """The spec the gateway would build for a premium invoice."""
    values = dict(
        kind="premium",
        item=record.premium_item,
        description=record.premium_description,
        amount=record.premium_amount,
        class_name=record.premium_class,
        template="Mckinney & Co.(Premium Payment)",
        ar_account="Accounts Receivable:Premium",
    )
    values.update(overrides)
    return InvoiceSpec(**values)


class PremiumInvoiceTests(unittest.TestCase):
    def build(self, record=PREMIUM, **overrides):
        xml = BUILDER.policy_invoice_add(record, premium_spec(record, **overrides), "JOB-1")
        return ET.fromstring(_body(xml)).find(".//InvoiceAdd")

    def test_it_follows_the_schema_order(self):
        add = self.build()
        # QuickBooks rejects the whole request if these are out of order; the
        # order comes from the SDK's own qbxmlops130.xml.
        self.assertEqual(
            [child.tag for child in add],
            [
                "CustomerRef", "ClassRef", "ARAccountRef", "TemplateRef",
                "TxnDate", "RefNumber", "Memo", "InvoiceLineAdd",
            ],
        )
        line = add.find("InvoiceLineAdd")
        self.assertEqual(
            [child.tag for child in line], ["ItemRef", "Desc", "Quantity", "ClassRef", "Amount"]
        )

    def test_it_bills_net_due_on_the_premium_account_and_template(self):
        add = self.build()
        self.assertEqual(add.findtext("CustomerRef/ListID"), "JOB-1")
        self.assertEqual(add.findtext("ARAccountRef/FullName"), "Accounts Receivable:Premium")
        self.assertEqual(add.findtext("TemplateRef/FullName"), "Mckinney & Co.(Premium Payment)")
        self.assertEqual(add.findtext("ClassRef/FullName"), "Full Payment")
        self.assertEqual(add.findtext("TxnDate"), "2026-09-01")
        line = add.find("InvoiceLineAdd")
        self.assertEqual(line.findtext("ItemRef/FullName"), "Non-Standard")
        # The premium invoice describes the premium, so Desc is 'Prem inv' -
        # not the 'Comm Inv' text, which belongs on the commission invoice.
        self.assertEqual(
            line.findtext("Desc"),
            "Premium Payment: 1234.5 & Commission: 160.55 & Policy Type: Renewal",
        )
        self.assertEqual(line.findtext("Quantity"), "1")
        self.assertEqual(line.findtext("Amount"), "1234.50")

    def test_a_standard_carrier_picks_the_standard_item(self):
        record = replace(PREMIUM, extra=(("carrier_type", "Standard"), ("net_due", "10")))
        self.assertEqual(self.build(record).find("InvoiceLineAdd").findtext("ItemRef/FullName"), "Standard")

    def test_a_long_policy_number_is_shortened_and_kept_in_the_memo(self):
        add = self.build()
        self.assertEqual(add.findtext("RefNumber"), "GL-2026-000")
        self.assertEqual(add.findtext("Memo"), "Policy #: GL-2026-000123")

    def test_transaction_type_and_source_use_the_renamed_template_fields(self):
        """The premium template renames Terms to "Transaction Type" and Rep to
        "Source", so those two built-in fields are what we actually write."""
        add = self.build(terms="Renewal", sales_rep="REFER")
        self.assertEqual(add.findtext("TermsRef/FullName"), "Renewal")
        self.assertEqual(add.findtext("SalesRepRef/FullName"), "REFER")
        # They sit between RefNumber and Memo in the schema.
        tags = [child.tag for child in add]
        self.assertEqual(
            tags,
            [
                "CustomerRef", "ClassRef", "ARAccountRef", "TemplateRef", "TxnDate",
                "RefNumber", "TermsRef", "SalesRepRef", "Memo", "InvoiceLineAdd",
            ],
        )

    def test_an_update_names_the_line_so_it_is_replaced_not_dropped(self):
        xml = BUILDER.policy_invoice_mod(
            PREMIUM,
            premium_spec(terms="Renewal", sales_rep="REFER"),
            "JOB-1", "TXN-1", "7", "TXN-1-1",
        )
        change = ET.fromstring(_body(xml)).find(".//InvoiceMod")
        self.assertEqual(
            [child.tag for child in change],
            [
                "TxnID", "EditSequence", "CustomerRef", "ClassRef", "ARAccountRef",
                "TemplateRef", "TxnDate", "RefNumber", "TermsRef", "SalesRepRef",
                "Memo", "InvoiceLineMod",
            ],
        )
        line = change.find("InvoiceLineMod")
        # Without the TxnLineID QuickBooks would delete the line and add a new one.
        self.assertEqual(line.findtext("TxnLineID"), "TXN-1-1")
        self.assertEqual(line.findtext("Amount"), "1234.50")

    def test_the_invoice_lookup_asks_for_lines(self):
        """An update needs the line's TxnLineID, which only comes with the lines."""
        request = ET.fromstring(_body(BUILDER.invoice_query("X"))).find(".//InvoiceQueryRq")
        self.assertEqual(request.findtext("IncludeLineItems"), "true")

    def test_the_job_note_carries_the_full_policy_number(self):
        xml = BUILDER.job_note_mod("JOB-1", "5", "Policy #: GL-2026-000123")
        change = ET.fromstring(_body(xml)).find(".//CustomerMod")
        # Only the Note is sent, so the job keeps its name and parent.
        self.assertEqual([c.tag for c in change], ["ListID", "EditSequence", "Notes"])
        self.assertEqual(change.findtext("Notes"), "Policy #: GL-2026-000123")

    def test_customer_lookups_ask_for_notes(self):
        """Without this the Note comes back empty and we would overwrite it."""
        for xml in (BUILDER.customer_query("Acme"), BUILDER.customer_query_by_id("1")):
            request = ET.fromstring(_body(xml)).find(".//CustomerQueryRq")
            asked = [n.text for n in request.findall("IncludeRetElement")]
            self.assertIn("Notes", asked)
            self.assertIn("EditSequence", asked)

    def test_transaction_type_comes_from_the_tab_not_the_policy_type_column(self):
        """The Type column (which tab the row came from) is always exactly
        "New Biz" or "Renewal"; the sheet's Policy type is free text that is
        often blank or says something else, so it is not used here."""
        renewal = replace(PREMIUM, source_tab="Renewal", extra=(("policy_type", "Rewrite"),))
        self.assertEqual(renewal.transaction_type, "Renewal")

        new_biz = replace(PREMIUM, source_tab="New Biz", extra=(("policy_type", ""),))
        self.assertEqual(new_biz.transaction_type, "New Biz")

        # Whatever the sheet's own column says is ignored.
        odd = replace(PREMIUM, source_tab="New Biz", extra=(("policy_type", "Razia winback"),))
        self.assertEqual(odd.transaction_type, "New Biz")

        self.assertEqual(replace(PREMIUM, source_tab="").transaction_type, "")

    def test_a_rep_initial_is_capped_at_five_characters(self):
        xml = BUILDER.sales_rep_add("EXISTINGLONG", "Existing insured - new project")
        add = ET.fromstring(_body(xml)).find(".//SalesRepAdd")
        self.assertEqual(add.findtext("Initial"), "EXIST")
        self.assertEqual(
            add.findtext("SalesRepEntityRef/FullName"), "Existing insured - new project"
        )

    def test_a_row_missing_its_class_or_carrier_type_is_held_back(self):
        record = replace(PREMIUM, extra=(("net_due", "500"),))
        reasons = " ".join(record.premium_reasons())
        self.assertIn("Carrier Type", reasons)
        self.assertIn("Class", reasons)
        self.assertNotIn("Net due", reasons)

    def test_zero_net_due_is_held_back(self):
        record = replace(
            PREMIUM, extra=(("carrier_type", "Standard"), ("policy_class", "Full Payment"))
        )
        self.assertIn("Net due", " ".join(record.premium_reasons()))


def _body(xml: str) -> str:
    return xml.split("?>", 2)[-1]


if __name__ == "__main__":
    unittest.main()
