import unittest
from datetime import date
from decimal import Decimal

from qb_agent.websource import WebSourceError, build_records

# A miniature version of the real sheet: two banner rows, then a header row that
# repeats 'Corporation Name' / 'Carrier' / 'Line of Business' in both the working
# block and the verified accounting block.
HEADER = [
    "Month", "Corporation Name", "Carrier", "Line of Business ", "Pure Prem",
    "policy type", "BULLING", "EFF", "Corporation Name", "Column 63", "Carrier",
    "Line of Business ", "Policy No#", "Pure Premium (Verfied)", "Commission %",
    "Commision", "Comm Inv",
]

BANNER_A = ["", "", "", "", "", "Mckinney AI Portal", "https://example.test"]
BANNER_B = ["", "", "Carrier Playbook"]


def row(working_corp, verified_corp, carrier, lob, policy, commission, description, eff):
    return [
        "January", working_corp, "OLD CARRIER", "OLD LOB", 1000,
        "new buiz", "DIRECT BILLING", eff, verified_corp, "", carrier,
        lob, policy, 1196, 0.175, commission, description,
    ]


ROWS = [
    BANNER_A,
    BANNER_B,
    HEADER,
    row("WORKING NAME", "SUNSHINE STORES INC", "Mid-Continent", "Pollution Liability",
        "04-TOP-70841", 209.2999999, "premium amount: 1196", "2026-01-05T19:00:00.000Z"),
    row("", "Summit Utah Retail LLC", "CRC USLI", "Liquor Liability",
        "LQ1023168", 96.625, "premium amount: 773", "2026-08-13T19:00:00.000Z"),
    row("ONLY WORKING", "", "", "", "", "", "", ""),  # no verified name -> skipped
]


class WebSourceTests(unittest.TestCase):
    def setUp(self):
        self.result = build_records("nb", ROWS)

    def test_banner_rows_are_skipped_and_the_header_is_found(self):
        self.assertEqual(len(self.result.records), 2)
        self.assertEqual(self.result.skipped_rows, 1)

    def test_the_verified_column_wins_over_the_working_one(self):
        record = self.result.records[0]
        self.assertEqual(record.customer_name, "SUNSHINE STORES INC")
        self.assertNotEqual(record.customer_name, "WORKING NAME")
        self.assertEqual(record.item, "Mid-Continent")
        self.assertNotEqual(record.item, "OLD CARRIER")
        self.assertEqual(record.job_name, "Pollution Liability")

    def test_policy_becomes_the_job_and_invoice_number(self):
        record = self.result.records[0]
        self.assertEqual(record.job_full_name, "SUNSHINE STORES INC:Pollution Liability")
        self.assertEqual(record.invoice_number, "04-TOP-70841")

    def test_renewal_keeps_old_and_new_policy_numbers_separate(self):
        header = list(HEADER)
        header.insert(4, "Policy Number")       # CRM block: expiring policy (R)
        renewal_row = row(
            "WORKING NAME", "SUNSHINE STORES INC", "Mid-Continent",
            "Pollution Liability", "NEW-900", 209.30, "renewal", "2026-01-05T19:00:00.000Z",
        )
        renewal_row.insert(4, "OLD-100")

        record = build_records("renewal", [header, renewal_row]).records[0]

        self.assertEqual(record.old_policy_number, "OLD-100")
        self.assertEqual(record.invoice_number, "NEW-900")

    def test_new_business_reads_location_from_crm_column_j(self):
        header = [
            "CRM 0", "CRM 1", "CRM 2", "CRM 3", "CRM 4", "CRM 5", "Status",
            "CRM 7", "CRM 8", "Location", "Policy Type", "EFF", "Corporation Name",
            "Carrier", "Line of Business", "Policy No#", "Commision", "Comm Inv", "Status",
        ]
        source_row = [
            "", "", "", "", "", "", "Bound", "", "",
            "Dpeg Retail LLC 1903 Hamilton Rd", "new biz", "2026-01-05",
            "Dpeg Retail LLC", "Carrier", "Commercial Property", "NEW-1903", 100, "commission", "Done",
        ]

        record = build_records("nb", [header, source_row]).records[0]

        self.assertEqual(record.location, "Dpeg Retail LLC 1903 Hamilton Rd")

    def test_commission_becomes_the_line_amount_rounded(self):
        self.assertEqual(self.result.records[0].amount, Decimal("209.30"))
        self.assertEqual(self.result.records[0].rate, Decimal("0"))
        self.assertEqual(self.result.records[0].quantity, Decimal("1"))

    def test_timezone_shifted_dates_round_to_the_day_the_user_typed(self):
        # 2026-01-05T19:00Z is midnight on the 6th in a UTC+5 sheet.
        self.assertEqual(self.result.records[0].invoice_date, date(2026, 1, 6))
        self.assertEqual(self.result.records[1].invoice_date, date(2026, 8, 14))

    def test_description_comes_from_comm_inv(self):
        self.assertEqual(self.result.records[0].description, "premium amount: 1196")

    def test_ids_are_stable_across_fetches(self):
        again = build_records("nb", ROWS)
        self.assertEqual(
            [r.source_id for r in self.result.records], [r.source_id for r in again.records]
        )

    def test_a_different_tab_gives_different_ids(self):
        other = build_records("renewal", ROWS)
        self.assertNotEqual(
            self.result.records[0].source_id, other.records[0].source_id
        )


class MissingCarrierHeaderTests(unittest.TestCase):
    """The 'renewal' tab leaves the carrier header blank."""

    def setUp(self):
        header = list(HEADER)
        header[10] = "Column 56"  # the carrier header, unnamed
        self.rows = [BANNER_A, BANNER_B, header] + ROWS[3:]

    def test_carrier_falls_back_to_the_column_left_of_line_of_business(self):
        result = build_records("renewal", self.rows)
        self.assertEqual(result.records[0].item, "Mid-Continent")

    def test_the_guess_is_reported_rather_than_silent(self):
        result = build_records("renewal", self.rows)
        self.assertTrue(any("Carrier" in warning for warning in result.warnings))


class BoundAndDoneTests(unittest.TestCase):
    """Only bound, finished policies belong in QuickBooks.

    The sheet has two 'Status' columns: the CRM one on the left (Bound / Not
    Bound / Dead Lead) and the accounting one in the verified block (DONE /
    in pkg / SHEET). A row has to satisfy both.
    """

    HEADER = [
        "Assign To", "Status", "Corporation Name",          # the working CRM block
        "policy type", "EFF", "Status", "Corporation Name",  # the verified block
        "Carrier", "Line of Business ", "Policy No#", "Commision", "Comm Inv",
    ]

    def row(self, bound, status, corp="ACME LLC"):
        return [
            "Amber", bound, corp,
            "new buiz", "2026-01-05T19:00:00.000Z", status, corp,
            "Travelers", "General Liability", "POL-1", 100, "Comm Inv",
        ]

    def build(self, *rows):
        return build_records("nb", [["", ""], ["", ""], self.HEADER, *rows])

    def test_only_bound_and_done_rows_are_loaded(self):
        result = self.build(
            self.row("Bound", "DONE", "KEEP ONE"),
            self.row("Bound", "done", "KEEP TWO"),
            self.row("Bound", "Done", "KEEP THREE"),
            self.row("Not Bound", "DONE", "DROP - not bound"),
            self.row("Dead Lead", "", "DROP - dead lead"),
            self.row("Bound", "in pkg", "DROP - not finished"),
            self.row("Bound", "SHEET", "DROP - still a sheet"),
            self.row("Bound", "", "DROP - no status"),
        )
        self.assertEqual(
            [record.customer_name for record in result.records],
            ["KEEP ONE", "KEEP TWO", "KEEP THREE"],
        )
        self.assertEqual(result.skipped_rows, 5)
        self.assertTrue(any("not bound and done" in text for text in result.warnings))

    def test_the_crm_status_is_read_from_the_left_of_the_verified_block(self):
        record = self.build(self.row("Bound", "DONE")).records[0]
        extra = dict(record.extra)
        self.assertEqual(extra["bound_status"], "Bound")   # the CRM column
        self.assertEqual(extra["sheet_status"], "DONE")    # the accounting column

    def test_a_sheet_without_a_crm_status_column_still_loads(self):
        """Older or simpler sheets have one block; they must not come back empty."""
        result = build_records("nb", ROWS)
        self.assertEqual(len(result.records), 2)
        self.assertTrue(any("cannot be limited" in text for text in result.warnings))


class HeaderlessExportTests(unittest.TestCase):
    """The feed can send a purpose-built grid with no header row at all.

    Its columns are matched by position, so the shape must be verified before
    it is trusted - reading the wrong column would put the wrong carrier and
    the wrong amount on a real invoice.
    """

    ROW = [
        "new buiz", "DIRECT BILLING", "Non-Standard", "NATA", "DONE", "1/6/2026",
        "SUNSHINE STORES INC", "SUNSHINE STORES INC", "Mid-Continent",
        "Pollution Liability", "04-TOP-000070841", "$1,196.00", "$1,196.00",
        "$0.00", "17.50%", "209.30", "premium amount: 1196", "$986.70",
        "Premium Payment: 986.7", "Agency Fees:0", "Full Payment",
    ]

    def build(self, *rows):
        return build_records("nb", [[""] * 21, *rows])

    def test_every_column_lands_where_it_belongs(self):
        record = self.build(self.ROW).records[0]
        extra = dict(record.extra)
        self.assertEqual(record.customer_name, "SUNSHINE STORES INC")
        self.assertEqual(record.job_name, "Pollution Liability")
        self.assertEqual(record.item, "Mid-Continent")          # the carrier
        self.assertEqual(record.invoice_number, "04-TOP-000070841")
        self.assertEqual(record.invoice_date, date(2026, 1, 6))
        self.assertEqual(record.amount, Decimal("209.30"))      # the commission
        self.assertEqual(record.description, "premium amount: 1196")
        self.assertEqual(extra["net_due"], "$986.70")
        self.assertEqual(extra["mckinney_fee"], "$0.00")
        self.assertEqual(extra["fees_invoice"], "Agency Fees:0")
        self.assertEqual(extra["carrier_type"], "Non-Standard")
        self.assertEqual(extra["policy_class"], "Full Payment")
        self.assertEqual(extra["lead_source"], "NATA")

    def test_this_export_is_already_bound_and_done_so_nothing_is_filtered(self):
        result = self.build(self.ROW, self.ROW[:6] + ["SECOND LLC"] + self.ROW[7:])
        self.assertEqual(len(result.records), 2)
        # No complaint about a missing CRM Status column: it was filtered upstream.
        self.assertFalse([w for w in result.warnings if "bound" in w])

    def test_a_changed_shape_is_refused_rather_than_guessed(self):
        for label, rows in (
            ("a column inserted", [self.ROW + ["extra"]] * 3),
            ("a column removed", [self.ROW[:-1]] * 3),
            ("no corporation name", [self.ROW[:6] + [""] + self.ROW[7:]] * 3),
            ("dates that are not dates", [self.ROW[:5] + ["n/a"] + self.ROW[6:]] * 3),
        ):
            with self.subTest(label):
                with self.assertRaises(WebSourceError):
                    self.build(*rows)


class TabLabelTests(unittest.TestCase):
    def test_rows_carry_the_tab_they_came_from(self):
        self.assertEqual(build_records("nb", ROWS).records[0].source_tab, "New Business")
        self.assertEqual(build_records("renewal", ROWS).records[0].source_tab, "Renewal")

    def test_renaming_the_label_keeps_the_saved_identity(self):
        """The saved history is keyed by the raw tab name, so the label can
        change without every policy losing what was done to it."""
        record = build_records("nb", ROWS).records[0]
        from qb_agent.importer import deterministic_source_id

        self.assertEqual(
            record.source_id,
            deterministic_source_id("web:nb", record.row_number, record.customer_name,
                                    record.invoice_number),
        )

    def test_an_unknown_tab_keeps_its_own_name(self):
        self.assertEqual(build_records("extra", ROWS).records[0].source_tab, "extra")


class BadInputTests(unittest.TestCase):
    def test_a_tab_without_a_corporation_column_is_rejected(self):
        with self.assertRaises(WebSourceError):
            build_records("x", [["Month", "Notes"], ["January", "hello"]])

    def test_a_tab_with_no_verified_names_is_rejected(self):
        rows = [BANNER_A, BANNER_B, HEADER, ROWS[5]]
        with self.assertRaises(WebSourceError):
            build_records("nb", rows)


if __name__ == "__main__":
    unittest.main()
