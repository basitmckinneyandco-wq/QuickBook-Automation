"""The shape of one row of work.

One uploaded spreadsheet row becomes one WorkRecord. A record always means
"make sure this customer exists"; if the row also carries invoice details it
means "and create this invoice for them".
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256

# QuickBooks rejects customer/job names and company names longer than this.
MAX_CUSTOMER_NAME = 41

# QuickBooks rejects an invoice RefNumber (what we use for the policy number)
# longer than this.
MAX_REF_NUMBER = 11


class Action(StrEnum):
    CUSTOMER_ONLY = "customer_only"
    CUSTOMER_AND_INVOICE = "customer_and_invoice"


class Operation(StrEnum):
    """What the dashboard asked for, which may be narrower than the row allows."""

    CREATE_CUSTOMER = "create_customer"   # customer, plus its policy job
    UPDATE_CUSTOMER = "update_customer"   # change a customer already in QuickBooks
    CREATE_INVOICE = "create_invoice"     # customer, job and invoice
    PREMIUM_INVOICE = "premium_invoice"   # the premium payment invoice on the job
    COMMISSION_INVOICE = "commission_invoice"   # the commission invoice on the job
    FEES_INVOICE = "fees_invoice"         # the handling fees invoice on the job

    @property
    def invoice_kind(self) -> str:
        """"premium" / "commission" for the invoice operations, else ""."""
        return INVOICE_KINDS.get(self, "")


# One policy can carry several invoices, tracked apart from one another.
INVOICE_KINDS = {
    Operation.PREMIUM_INVOICE: "premium",
    Operation.COMMISSION_INVOICE: "commission",
    Operation.FEES_INVOICE: "fees",
}


@dataclass(frozen=True, slots=True)
class InvoiceSpec:
    """What actually goes on one invoice, whichever kind it is.

    The kinds differ only in these values - the same builder writes them all -
    so a new kind is a new spec rather than a new code path.
    """

    kind: str
    item: str
    description: str
    amount: Decimal
    class_name: str = ""
    template: str = ""
    ar_account: str = ""
    # The template's "Transaction Type", which is QuickBooks' Ship Via field.
    transaction_type: str = ""
    sales_rep: str = ""
    # What goes in the invoice's number box, when it is not the policy number.
    ref_number: str = ""
    # The Memo, when this kind always carries one.
    memo: str = ""


@dataclass(frozen=True, slots=True)
class WorkRecord:
    source_id: str
    row_number: int
    customer_name: str
    job_name: str = ""
    company_name: str = ""
    email: str = ""
    phone: str = ""
    invoice_number: str = ""
    invoice_date: date | None = None
    item: str = ""
    description: str = ""
    quantity: Decimal = Decimal("1")
    rate: Decimal = Decimal("0")
    amount: Decimal = Decimal("0")
    source_tab: str = ""  # "New Biz" / "Renewal" - which sheet tab the row came from
    # Every other verified column from the sheet, kept verbatim for display only.
    # A tuple of pairs rather than a dict so the record stays frozen and ordered.
    extra: tuple[tuple[str, str], ...] = ()

    @property
    def wants_invoice(self) -> bool:
        """A row is an invoice row as soon as it carries any invoice detail."""
        return bool(self.invoice_number or self.item or self.amount or self.rate)

    @property
    def action(self) -> Action:
        return Action.CUSTOMER_AND_INVOICE if self.wants_invoice else Action.CUSTOMER_ONLY

    @property
    def has_job(self) -> bool:
        """A policy row puts the invoice on a job underneath the customer."""
        return bool(self.job_name.strip())

    @property
    def job_full_name(self) -> str:
        """The source-system name shown in the dashboard."""
        return f"{self.customer_name}:{self.job_name}" if self.has_job else self.customer_name

    @property
    def quickbooks_customer_name(self) -> str:
        """The customer name QuickBooks can store; the full value goes in Notes."""
        return _fit_name(self.customer_name, MAX_CUSTOMER_NAME)

    @property
    def customer_name_was_shortened(self) -> bool:
        return self.quickbooks_customer_name != self.customer_name

    @property
    def quickbooks_job_name(self) -> str:
        """The Line of Business as QuickBooks will name the job."""
        # Same 41-character ceiling as a customer: a job is an entry on the
        # very same list, so a long Line of Business would be rejected too.
        return _fit_name(self.job_name, MAX_CUSTOMER_NAME)

    @property
    def quickbooks_job_full_name(self) -> str:
        """How QuickBooks names a job under its shortened customer name."""
        customer = self.quickbooks_customer_name
        return f"{customer}:{self.quickbooks_job_name}" if self.has_job else customer

    @property
    def quickbooks_company_name(self) -> str:
        """The company name QuickBooks can store; the full value goes in Notes."""
        return _fit_name(self.company_name, MAX_CUSTOMER_NAME)

    @property
    def company_name_was_shortened(self) -> bool:
        return bool(self.company_name) and self.quickbooks_company_name != self.company_name

    @property
    def quickbooks_invoice_number(self) -> str:
        """The invoice number QuickBooks can store; the full value goes in the Memo."""
        return self.invoice_number[:MAX_REF_NUMBER]

    @property
    def invoice_number_was_shortened(self) -> bool:
        return self.quickbooks_invoice_number != self.invoice_number

    @property
    def line_total(self) -> Decimal:
        return self.amount if self.amount else self.quantity * self.rate

    # --- the premium invoice ------------------------------------------------
    #
    # Its values come from the display-only sheet columns rather than the
    # QuickBooks-facing ones: the amount is "net due", not the commission.

    def _extra(self, key: str) -> str:
        return dict(self.extra).get(key, "").strip()

    @property
    def premium_item(self) -> str:
        """Carrier Type decides the QuickBooks item: Standard or Non-Standard."""
        value = self._extra("carrier_type").casefold()
        if not value:
            return ""
        return "Non-Standard" if "non" in value else "Standard"

    @property
    def premium_class(self) -> str:
        return self._extra("policy_class")

    @property
    def premium_description(self) -> str:
        """The premium invoice describes the premium, so it uses 'Prem inv'.

        'Comm Inv' describes the commission and belongs on the commission
        invoice; it is only the fallback here for rows where Prem inv is blank.
        """
        return self._extra("prem_inv") or self.description

    def _extra_money(self, key: str) -> Decimal:
        """A sheet column read as money, tolerating commas, $ and blanks."""
        raw = self._extra(key).replace(",", "").replace("$", "")
        try:
            return Decimal(raw).quantize(Decimal("0.01")) if raw else Decimal("0")
        except (ArithmeticError, ValueError):
            return Decimal("0")

    @property
    def premium_amount(self) -> Decimal:
        """The premium invoice bills 'net due', not the commission."""
        return self._extra_money("net_due")

    @property
    def transaction_type(self) -> str:
        """The row's own Policy type column, which is the Transaction Type.

        That column is a dropdown holding the same words as QuickBooks' list,
        so the row states its transaction type outright. The tab it arrived on
        cannot: every row on the renewal tab looks like a renewal, even the
        ones that are really a rewrite, an endorsement or a cancellation.

        A row whose column is still blank falls back to the tab, so rows filled
        in before the dropdown existed keep working.
        """
        chosen = self._extra("policy_type")
        if chosen:
            return chosen
        return self.tab_transaction_type

    @property
    def tab_transaction_type(self) -> str:
        """What the tab alone says the row is - the fallback, not the answer."""
        value = self.source_tab.strip().casefold()
        if not value:
            return ""
        if "renew" in value:
            return "Renewal"
        if "buiz" in value or "biz" in value or "new" in value:
            return "New Biz"
        return self.source_tab.strip()

    @property
    def lead_source(self) -> str:
        return self._extra("lead_source")

    # --- the commission invoice ---------------------------------------------
    #
    # Same job, same policy number, but it bills what the agency earned: the
    # item is the carrier, the amount is the commission.

    @property
    def commission_item(self) -> str:
        """The carrier becomes the QuickBooks item on a commission invoice."""
        return self.item.strip()

    @property
    def commission_description(self) -> str:
        return self.description

    @property
    def commission_amount(self) -> Decimal:
        return self.amount

    def commission_reasons(self) -> list[str]:
        """Everything that stops a commission invoice being raised."""
        reasons = self.customer_reasons()
        if self.invoice_date is None:
            reasons.append("Effective date is missing, so the invoice has no date.")
        if not self.commission_item:
            reasons.append("Carrier is empty, so the invoice item cannot be set.")
        if not self.premium_class:
            reasons.append("Class is empty, so the invoice class cannot be set.")
        if self.commission_amount <= 0:
            reasons.append("Commission must be greater than zero to raise a commission invoice.")
        return reasons

    # --- the handling fees invoice ------------------------------------------
    #
    # The item is always "Fees"; what varies is the agency fee being billed.

    @property
    def fees_description(self) -> str:
        return self._extra("fees_invoice")

    @property
    def fees_amount(self) -> Decimal:
        return self._extra_money("mckinney_fee")

    def fees_reasons(self) -> list[str]:
        """Everything that stops a handling fees invoice being raised."""
        reasons = self.customer_reasons()
        if self.invoice_date is None:
            reasons.append("Effective date is missing, so the invoice has no date.")
        if not self.premium_class:
            reasons.append("Class is empty, so the invoice class cannot be set.")
        if self.fees_amount <= 0:
            reasons.append("McKinney Fee must be greater than zero to raise a fees invoice.")
        return reasons

    def invoice_reasons(self, kind: str) -> list[str]:
        if kind == "commission":
            return self.commission_reasons()
        if kind == "fees":
            return self.fees_reasons()
        return self.premium_reasons()

    def invoice_applies(self, kind: str) -> bool:
        """Is there anything to bill for this kind of invoice at all?

        A policy that earns no commission needs no commission invoice, and one
        with no agency fee needs no fees invoice. That is not a row waiting to
        be fixed - there is simply nothing to raise, and calling it "needs
        review" sends somebody looking for a mistake that was never made.

        The premium always applies: every policy has one, and a net due of zero
        really is something to look into.
        """
        if kind == "commission":
            return self.commission_amount > 0
        if kind == "fees":
            return self.fees_amount > 0
        return True

    def premium_reasons(self) -> list[str]:
        """Everything that stops a premium invoice being raised."""
        reasons = self.customer_reasons()
        if self.invoice_date is None:
            reasons.append("Effective date is missing, so the invoice has no date.")
        if not self.premium_item:
            reasons.append(
                "Carrier Type is empty, so the invoice item cannot be set to "
                "Standard or Non-Standard."
            )
        if not self.premium_class:
            reasons.append("Class is empty, so the invoice class cannot be set.")
        if self.premium_amount <= 0:
            reasons.append("Net due must be greater than zero to raise a premium invoice.")
        return reasons

    def customer_reasons(self) -> list[str]:
        """Only what stops the CUSTOMER (and its job) from being created.

        The policy number blocks this too, even though it's really an invoice
        field: without it there is nothing to tell this policy apart from a
        second one for the same customer later, so a customer/job created now
        could end up orphaned or duplicated once the real policy number arrives.
        """
        return [
            reason
            for reason in self.review_reasons()
            if reason.startswith(("Customer name", "Job name", "Policy number"))
        ]

    def review_reasons(self) -> list[str]:
        """Everything a human must fix before this row may be sent to QuickBooks."""
        reasons: list[str] = []
        if not self.customer_name.strip():
            reasons.append("Customer name is missing.")
        if len(self.job_name) > MAX_CUSTOMER_NAME:
            reasons.append(
                f"Job name is longer than the {MAX_CUSTOMER_NAME} characters QuickBooks allows."
            )
        if self.action is Action.CUSTOMER_AND_INVOICE:
            if not self.invoice_number.strip():
                reasons.append(
                    "Policy number is missing; it becomes the invoice number, so without it "
                    "the same invoice could be created twice."
                )
            if self.invoice_date is None:
                reasons.append("Invoice date is missing or is not in a date format we recognise.")
            if not self.item.strip():
                reasons.append(
                    "Item is missing; QuickBooks needs an item that already exists in its Item List."
                )
            if self.line_total <= 0:
                reasons.append("Amount must be greater than zero.")
        return reasons

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["invoice_date"] = self.invoice_date.isoformat() if self.invoice_date else ""
        result["quantity"] = str(self.quantity)
        result["rate"] = str(self.rate)
        result["amount"] = str(self.amount)
        result["line_total"] = str(self.line_total)
        result["action"] = self.action.value
        result["job_full_name"] = self.job_full_name
        result["quickbooks_customer_name"] = self.quickbooks_customer_name
        result["customer_name_was_shortened"] = self.customer_name_was_shortened
        result["company_name_was_shortened"] = self.company_name_was_shortened
        result["quickbooks_invoice_number"] = self.quickbooks_invoice_number
        result["invoice_number_was_shortened"] = self.invoice_number_was_shortened
        result["extra"] = dict(self.extra)
        result["review_reasons"] = self.review_reasons()
        result["premium_item"] = self.premium_item
        result["premium_class"] = self.premium_class
        result["premium_description"] = self.premium_description
        result["premium_amount"] = str(self.premium_amount)
        result["transaction_type"] = self.transaction_type
        result["premium_reasons"] = self.premium_reasons()
        result["commission_item"] = self.commission_item
        result["commission_amount"] = str(self.commission_amount)
        result["commission_reasons"] = self.commission_reasons()
        result["fees_amount"] = str(self.fees_amount)
        result["fees_reasons"] = self.fees_reasons()
        return result


def _tidy_name(value: str) -> str:
    """A name fit for a QuickBooks list entry.

    Two things QuickBooks will not accept in a name:

    * A colon, which it reserves to separate a customer from its job. Plenty
      of corporation names carry one ("… DBA: Food Mart"), so it becomes a
      dash - the name still reads the same to a person.
    * A line break or tab, which sheet cells pick up from Alt+Enter. A list
      name spanning two lines is one nobody can search for or match again.

    The untouched original still goes into the QuickBooks Notes field, so
    nothing is lost by tidying what goes in the name.
    """
    return " ".join(value.replace(":", " - ").split())


def _fit_name(value: str, limit: int) -> str:
    """A tidy name cut to QuickBooks' length limit, without a trailing space.

    Cutting at a fixed length lands mid-word as often as not, and when it lands
    on a space the name we hold ends in one while the name QuickBooks stores
    does not - it trims. Every later lookup then asks for a name that is one
    character longer than the one on file, finds nothing, tries to create it
    again and is told the name is already in use (error 3100).
    """
    return _tidy_name(value)[:limit].rstrip()


def deterministic_source_id(
    source_label: str,
    row_number: int,
    customer_name: str,
    invoice_number: str,
) -> str:
    """A stable ID for a row, so re-uploading the same file recognises the same work."""
    material = "|".join(
        part.strip().casefold()
        for part in (source_label, str(row_number), customer_name, invoice_number)
    )
    return f"ROW-{sha256(material.encode('utf-8')).hexdigest()[:16].upper()}"
