import unittest
from datetime import date
from decimal import Decimal

from qb_agent.importer import ImportError_, load_records

CSV = (
    "Customer Name,Invoice Number,Invoice Date,Item,Amount\n"
    "Acme Roofing,INV-1,09/01/2026,Services,\"$1,200.00\"\n"
    "Bluebird Cafe,INV-2,2026-09-02,Services,(50)\n"
    "\n"
    ",INV-3,09/03/2026,Services,10\n"
).encode("utf-8")


class ImporterTests(unittest.TestCase):
    def test_reads_rows_and_cleans_values(self):
        result = load_records("book.csv", CSV)
        self.assertEqual(len(result.records), 3)
        first = result.records[0]
        self.assertEqual(first.customer_name, "Acme Roofing")
        self.assertEqual(first.invoice_date, date(2026, 9, 1))
        self.assertEqual(first.amount, Decimal("1200.00"))
        self.assertEqual(result.skipped_rows, 1)

    def test_parentheses_mean_negative(self):
        result = load_records("book.csv", CSV)
        self.assertEqual(result.records[1].amount, Decimal("-50"))

    def test_missing_customer_is_flagged_not_dropped(self):
        result = load_records("book.csv", CSV)
        self.assertIn("Customer name is missing.", result.records[2].review_reasons())

    def test_quantity_times_rate_fills_the_amount(self):
        data = b"Customer,Invoice No,Date,Item,Qty,Rate\nAcme,INV-9,09/01/2026,Services,3,100\n"
        record = load_records("book.csv", data).records[0]
        self.assertEqual(record.amount, Decimal("300.00"))

    def test_header_aliases_are_accepted(self):
        data = b"Client Name,Invoice #,Txn Date,Service,Total\nAcme,INV-7,09/01/2026,Services,25\n"
        record = load_records("book.csv", data).records[0]
        self.assertEqual(record.customer_name, "Acme")
        self.assertEqual(record.invoice_number, "INV-7")

    def test_file_without_a_customer_column_is_rejected(self):
        with self.assertRaises(ImportError_):
            load_records("book.csv", b"Colour,Size\nred,large\n")

    def test_unknown_extension_is_rejected(self):
        with self.assertRaises(ImportError_):
            load_records("book.pdf", b"anything")

    def test_same_file_gives_the_same_ids(self):
        first = load_records("book.csv", CSV).records
        second = load_records("book.csv", CSV).records
        self.assertEqual([r.source_id for r in first], [r.source_id for r in second])


if __name__ == "__main__":
    unittest.main()
