"""Build the qbXML "forms" QuickBooks accepts, and read its replies back.

Nothing here talks to QuickBooks. These functions only produce text (a request)
and turn text (a response) into small Python objects. That makes every request
shape testable without QuickBooks being installed at all.

Element order matters in qbXML: QuickBooks rejects a request whose child
elements are out of the order defined in its schema, so do not reshuffle these.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from xml.etree import ElementTree as ET

from .models import MAX_CUSTOMER_NAME, InvoiceSpec, WorkRecord

# QuickBooks list limits for the values these invoices reference.
MAX_REP_INITIAL = 5
MAX_ITEM_NAME = 31


@dataclass(frozen=True, slots=True)
class QbResponse:
    status_code: int
    status_severity: str
    status_message: str
    list_id: str | None = None
    full_name: str | None = None
    edit_sequence: str | None = None
    txn_id: str | None = None
    ref_number: str | None = None
    customer_list_id: str | None = None
    notes: str | None = None
    line_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.status_code == 0

    @property
    def not_found(self) -> bool:
        """QuickBooks' way of saying "no such record" in reply to a query."""
        message = self.status_message.casefold()
        if self.status_code == 1:
            return True
        return self.status_code == 500 and (
            "could not be found" in message or "cannot be found" in message
        )


@dataclass(frozen=True, slots=True)
class QbxmlBuilder:
    version: str = "13.0"

    def customer_query(self, full_name: str, request_id: str = "customer-query") -> str:
        root, request = self._envelope("CustomerQueryRq", request_id)
        ET.SubElement(request, "FullName").text = full_name
        # EditSequence is required before any CustomerMod can be sent, and
        # Notes has to be asked for or it comes back empty and gets overwritten.
        for element in ("ListID", "FullName", "EditSequence", "Notes"):
            ET.SubElement(request, "IncludeRetElement").text = element
        return self._serialize(root)

    def customer_query_by_id(self, list_id: str, request_id: str = "customer-by-id") -> str:
        """Look a customer or job up by ListID, to see what it is called now."""
        root, request = self._envelope("CustomerQueryRq", request_id)
        ET.SubElement(request, "ListID").text = list_id
        for element in ("ListID", "FullName", "EditSequence", "Notes"):
            ET.SubElement(request, "IncludeRetElement").text = element
        return self._serialize(root)

    def job_mod(self, record: WorkRecord, list_id: str, edit_sequence: str,
                request_id: str = "job-mod") -> str:
        """Rename a job in place when its Line of Business changed.

        Only Name is sent: the job keeps its ParentRef, so it stays under the
        same customer and keeps any invoices already raised against it.
        """
        root, request = self._envelope("CustomerModRq", request_id)
        change = ET.SubElement(request, "CustomerMod")
        ET.SubElement(change, "ListID").text = list_id
        ET.SubElement(change, "EditSequence").text = edit_sequence
        ET.SubElement(change, "Name").text = record.quickbooks_job_name
        return self._serialize(root)

    def job_note_mod(self, list_id: str, edit_sequence: str, notes: str,
                     request_id: str = "job-note") -> str:
        """Put text in a job's Note box, leaving everything else untouched."""
        root, request = self._envelope("CustomerModRq", request_id)
        change = ET.SubElement(request, "CustomerMod")
        ET.SubElement(change, "ListID").text = list_id
        ET.SubElement(change, "EditSequence").text = edit_sequence
        ET.SubElement(change, "Notes").text = notes
        return self._serialize(root)

    def customer_mod(self, record: WorkRecord, list_id: str, edit_sequence: str,
                     request_id: str = "customer-mod") -> str:
        root, request = self._envelope("CustomerModRq", request_id)
        change = ET.SubElement(request, "CustomerMod")
        # Schema order: ListID, EditSequence, Name, ..., CompanyName, ..., Phone, ..., Email.
        ET.SubElement(change, "ListID").text = list_id
        ET.SubElement(change, "EditSequence").text = edit_sequence
        ET.SubElement(change, "Name").text = record.quickbooks_customer_name
        if record.company_name:
            ET.SubElement(change, "CompanyName").text = record.quickbooks_company_name
        if record.phone:
            ET.SubElement(change, "Phone").text = record.phone
        if record.email:
            ET.SubElement(change, "Email").text = record.email
        if record.customer_name_was_shortened or record.company_name_was_shortened:
            ET.SubElement(change, "Notes").text = record.customer_name
        return self._serialize(root)

    def customer_add(self, record: WorkRecord, request_id: str = "customer-add") -> str:
        root, request = self._envelope("CustomerAddRq", request_id)
        addition = ET.SubElement(request, "CustomerAdd")
        # Schema order: Name, CompanyName, ..., Phone, ..., Email, ..., Notes.
        ET.SubElement(addition, "Name").text = record.quickbooks_customer_name
        if record.company_name:
            ET.SubElement(addition, "CompanyName").text = record.quickbooks_company_name
        if record.phone:
            ET.SubElement(addition, "Phone").text = record.phone
        if record.email:
            ET.SubElement(addition, "Email").text = record.email
        if record.customer_name_was_shortened or record.company_name_was_shortened:
            ET.SubElement(addition, "Notes").text = record.customer_name
        return self._serialize(root)

    def job_add(self, record: WorkRecord, parent_list_id: str, request_id: str = "job-add") -> str:
        """A job is just a customer with a ParentRef — the same CustomerAddRq."""
        root, request = self._envelope("CustomerAddRq", request_id)
        addition = ET.SubElement(request, "CustomerAdd")
        # Schema order: Name, IsActive, ClassRef, ParentRef, CompanyName, ...
        ET.SubElement(addition, "Name").text = record.quickbooks_job_name
        parent = ET.SubElement(addition, "ParentRef")
        ET.SubElement(parent, "ListID").text = parent_list_id
        return self._serialize(root)

    def policy_invoice_add(
        self,
        record: WorkRecord,
        spec: InvoiceSpec,
        job_list_id: str,
        request_id: str = "policy-invoice",
    ) -> str:
        """An invoice raised against the policy's job.

        Differs from a plain invoice: it carries a class, posts to a specific
        A/R sub-account and uses that kind's template. What varies between a
        premium and a commission invoice is all in the spec.
        """
        root, request = self._envelope("InvoiceAddRq", request_id)
        addition = ET.SubElement(request, "InvoiceAdd")
        self._invoice_body(addition, record, spec, job_list_id)
        # Line order: ItemRef, Desc, Quantity, ... ClassRef, Amount.
        self._invoice_line(ET.SubElement(addition, "InvoiceLineAdd"), spec)
        return self._serialize(root)

    def policy_invoice_mod(
        self,
        record: WorkRecord,
        spec: InvoiceSpec,
        job_list_id: str,
        txn_id: str,
        edit_sequence: str,
        txn_line_id: str,
        request_id: str = "policy-invoice-mod",
    ) -> str:
        """Bring an existing invoice back in line with the sheet.

        The single line is modified by its TxnLineID rather than added: in an
        InvoiceMod any line left out is deleted, so naming it is what keeps
        QuickBooks from replacing the line and losing its history.
        """
        root, request = self._envelope("InvoiceModRq", request_id)
        change = ET.SubElement(request, "InvoiceMod")
        ET.SubElement(change, "TxnID").text = txn_id
        ET.SubElement(change, "EditSequence").text = edit_sequence
        self._invoice_body(change, record, spec, job_list_id)
        line = ET.SubElement(change, "InvoiceLineMod")
        ET.SubElement(line, "TxnLineID").text = txn_line_id
        self._invoice_line(line, spec)
        return self._serialize(root)

    def _invoice_body(
        self,
        addition: ET.Element,
        record: WorkRecord,
        spec: InvoiceSpec,
        job_list_id: str,
    ) -> None:
        """Everything above the line items, shared by the add and the mod."""
        # Schema order: CustomerRef, ClassRef, ARAccountRef, TemplateRef,
        # TxnDate, RefNumber, SalesRepRef, ShipMethodRef, Memo, then the lines.
        customer_ref = ET.SubElement(addition, "CustomerRef")
        ET.SubElement(customer_ref, "ListID").text = job_list_id
        if spec.class_name:
            class_ref = ET.SubElement(addition, "ClassRef")
            ET.SubElement(class_ref, "FullName").text = spec.class_name
        if spec.ar_account:
            account_ref = ET.SubElement(addition, "ARAccountRef")
            ET.SubElement(account_ref, "FullName").text = spec.ar_account
        if spec.template:
            template_ref = ET.SubElement(addition, "TemplateRef")
            ET.SubElement(template_ref, "FullName").text = spec.template
        if record.invoice_date is not None:
            ET.SubElement(addition, "TxnDate").text = record.invoice_date.isoformat()
        if spec.ref_number:
            ET.SubElement(addition, "RefNumber").text = spec.ref_number
        elif record.invoice_number:
            ET.SubElement(addition, "RefNumber").text = record.quickbooks_invoice_number
        # Both templates rename two built-in fields, so that is where these
        # land: Rep is labelled "Source", and Ship Via is "Transaction Type".
        #
        # Ship Via, not Terms. The label on the form says nothing about which
        # field is underneath it, and Terms is the one that looks right - but
        # the list behind this company's Transaction Type holds Endorsement,
        # Cancellation and Rewrite, which are not payment terms. Written to
        # Terms the value is stored and never shown, because the template does
        # not display Terms at all.
        if spec.sales_rep:
            rep_ref = ET.SubElement(addition, "SalesRepRef")
            ET.SubElement(rep_ref, "FullName").text = spec.sales_rep
        if spec.transaction_type:
            ship_ref = ET.SubElement(addition, "ShipMethodRef")
            ET.SubElement(ship_ref, "FullName").text = spec.transaction_type
        if spec.memo:
            ET.SubElement(addition, "Memo").text = spec.memo
        elif record.invoice_number_was_shortened:
            ET.SubElement(addition, "Memo").text = f"Policy #: {record.invoice_number}"

    def _invoice_line(self, line: ET.Element, spec: InvoiceSpec) -> None:
        item_ref = ET.SubElement(line, "ItemRef")
        ET.SubElement(item_ref, "FullName").text = spec.item
        if spec.description:
            ET.SubElement(line, "Desc").text = spec.description
        ET.SubElement(line, "Quantity").text = "1"
        if spec.class_name:
            line_class = ET.SubElement(line, "ClassRef")
            ET.SubElement(line_class, "FullName").text = spec.class_name
        ET.SubElement(line, "Amount").text = _money(spec.amount)

    def service_item_add(
        self, name: str, income_account: str, request_id: str = "item-add"
    ) -> str:
        """Carriers arrive as free text, so their items are created on demand."""
        root, request = self._envelope("ItemServiceAddRq", request_id)
        addition = ET.SubElement(request, "ItemServiceAdd")
        ET.SubElement(addition, "Name").text = name[:MAX_ITEM_NAME]
        # The schema's OR groups are choices, not wrapper elements, so
        # SalesOrPurchase sits directly under ItemServiceAdd.
        sales = ET.SubElement(addition, "SalesOrPurchase")
        account_ref = ET.SubElement(sales, "AccountRef")
        ET.SubElement(account_ref, "FullName").text = income_account
        return self._serialize(root)

    def service_item_list(self, request_id: str = "item-list") -> str:
        root, _ = self._envelope("ItemServiceQueryRq", request_id)
        return self._serialize(root)

    def account_list(self, request_id: str = "account-list") -> str:
        root, _ = self._envelope("AccountQueryRq", request_id)
        return self._serialize(root)

    def template_list(self, request_id: str = "template-list") -> str:
        root, _ = self._envelope("TemplateQueryRq", request_id)
        return self._serialize(root)

    def class_list(self, request_id: str = "class-list") -> str:
        root, _ = self._envelope("ClassQueryRq", request_id)
        return self._serialize(root)

    # The two renamed template fields are list references, so their values have
    # to exist in QuickBooks before an invoice can point at them.

    def ship_method_list(self, request_id: str = "ship-method-list") -> str:
        """The Ship Via list, which the invoice templates label Transaction Type."""
        root, _ = self._envelope("ShipMethodQueryRq", request_id)
        return self._serialize(root)

    def sales_rep_list(self, request_id: str = "rep-list") -> str:
        root, _ = self._envelope("SalesRepQueryRq", request_id)
        return self._serialize(root)

    def other_name_add(self, name: str, request_id: str = "other-name-add") -> str:
        """A sales rep must point at a person, and an Other Name is the
        lightest kind QuickBooks will accept."""
        root, request = self._envelope("OtherNameAddRq", request_id)
        addition = ET.SubElement(request, "OtherNameAdd")
        ET.SubElement(addition, "Name").text = name[:MAX_CUSTOMER_NAME]
        return self._serialize(root)

    def sales_rep_add(
        self, initial: str, entity_name: str, request_id: str = "rep-add"
    ) -> str:
        root, request = self._envelope("SalesRepAddRq", request_id)
        addition = ET.SubElement(request, "SalesRepAdd")
        ET.SubElement(addition, "Initial").text = initial[:MAX_REP_INITIAL]
        ET.SubElement(addition, "IsActive").text = "true"
        entity = ET.SubElement(addition, "SalesRepEntityRef")
        ET.SubElement(entity, "FullName").text = entity_name[:MAX_CUSTOMER_NAME]
        return self._serialize(root)

    def invoice_query(self, ref_number: str, request_id: str = "invoice-query") -> str:
        root, request = self._envelope("InvoiceQueryRq", request_id)
        ET.SubElement(request, "RefNumber").text = ref_number
        # The lines come back so an existing invoice can be modified in place.
        ET.SubElement(request, "IncludeLineItems").text = "true"
        return self._serialize(root)

    def invoice_query_for_job(self, job_list_id: str, request_id: str = "invoice-job") -> str:
        """Every invoice on one customer:job.

        Used where the number cannot identify the invoice - fees invoices are
        all numbered "Fee" - so the job narrows it and the Memo decides.
        QuickBooks does not allow a RefNumber and an EntityFilter in the same
        query, so the number is checked afterwards rather than asked for.
        """
        root, request = self._envelope("InvoiceQueryRq", request_id)
        entity = ET.SubElement(request, "EntityFilter")
        ET.SubElement(entity, "ListID").text = job_list_id
        ET.SubElement(request, "IncludeLineItems").text = "true"
        return self._serialize(root)

    def invoice_add(
        self,
        record: WorkRecord,
        customer_list_id: str,
        template_name: str = "",
        request_id: str = "invoice-add",
    ) -> str:
        root, request = self._envelope("InvoiceAddRq", request_id)
        addition = ET.SubElement(request, "InvoiceAdd")
        # Schema order: CustomerRef, TemplateRef, TxnDate, RefNumber, then lines.
        customer_ref = ET.SubElement(addition, "CustomerRef")
        ET.SubElement(customer_ref, "ListID").text = customer_list_id
        if template_name:
            template_ref = ET.SubElement(addition, "TemplateRef")
            ET.SubElement(template_ref, "FullName").text = template_name
        if record.invoice_date is not None:
            ET.SubElement(addition, "TxnDate").text = record.invoice_date.isoformat()
        if record.invoice_number:
            ET.SubElement(addition, "RefNumber").text = record.quickbooks_invoice_number
        # Memo comes before the line items in the InvoiceAdd schema.
        if record.invoice_number_was_shortened:
            ET.SubElement(addition, "Memo").text = f"Policy #: {record.invoice_number}"

        line = ET.SubElement(addition, "InvoiceLineAdd")
        # Schema order: ItemRef, Desc, Quantity, Rate, Amount.
        item_ref = ET.SubElement(line, "ItemRef")
        ET.SubElement(item_ref, "FullName").text = record.item
        if record.description:
            ET.SubElement(line, "Desc").text = record.description
        if record.rate > 0:
            # Give QuickBooks quantity x rate and let it compute the amount itself.
            ET.SubElement(line, "Quantity").text = _number(record.quantity)
            ET.SubElement(line, "Rate").text = _money(record.rate)
        else:
            ET.SubElement(line, "Amount").text = _money(record.line_total)
        return self._serialize(root)

    def _envelope(self, message_name: str, request_id: str) -> tuple[ET.Element, ET.Element]:
        root = ET.Element("QBXML")
        messages = ET.SubElement(root, "QBXMLMsgsRq", {"onError": "stopOnError"})
        request = ET.SubElement(messages, message_name, {"requestID": request_id})
        return root, request

    def _serialize(self, root: ET.Element) -> str:
        body = ET.tostring(root, encoding="unicode", short_empty_elements=True)
        return f'<?xml version="1.0" encoding="utf-8"?>\n<?qbxml version="{self.version}"?>\n{body}'


def parse_response(xml: str) -> list[QbResponse]:
    root = ET.fromstring(xml)
    responses: list[QbResponse] = []
    for node in root.findall(".//QBXMLMsgsRs/*"):
        returned = next((child for child in node if child.tag.endswith("Ret")), None)
        responses.append(
            QbResponse(
                status_code=int(node.attrib.get("statusCode", "-1")),
                status_severity=node.attrib.get("statusSeverity", "Error"),
                status_message=node.attrib.get("statusMessage", "Unknown QuickBooks response"),
                list_id=_find_text(returned, "ListID"),
                full_name=_find_text(returned, "FullName"),
                edit_sequence=_find_text(returned, "EditSequence"),
                txn_id=_find_text(returned, "TxnID"),
                ref_number=_find_text(returned, "RefNumber"),
                customer_list_id=_find_text(returned, "CustomerRef/ListID"),
                notes=_find_text(returned, "Notes"),
                line_id=_find_text(returned, "InvoiceLineRet/TxnLineID"),
            )
        )
    return responses


def parse_invoice_matches(xml: str) -> list[dict[str, str]]:
    """Every invoice carrying a given number, with what tells them apart.

    A policy's premium and commission invoices share its policy number, so the
    A/R account is what says which one we are looking at.
    """
    return [
        {
            "txn_id": ret.findtext("TxnID", ""),
            "ref_number": ret.findtext("RefNumber", ""),
            "edit_sequence": ret.findtext("EditSequence", ""),
            "customer_list_id": ret.findtext("CustomerRef/ListID", ""),
            "ar_account": ret.findtext("ARAccountRef/FullName", ""),
            "line_id": ret.findtext("InvoiceLineRet/TxnLineID", ""),
            "memo": ret.findtext("Memo", ""),
        }
        for ret in ET.fromstring(xml).iter("InvoiceRet")
    ]


def _find_text(node: ET.Element | None, tag: str) -> str | None:
    if node is None:
        return None
    value = node.findtext(tag)
    return value.strip() if value else None


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):f}"


def _number(value: Decimal) -> str:
    return f"{value:f}"
