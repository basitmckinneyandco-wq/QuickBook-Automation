"""The connection layer: the only place that actually touches QuickBooks.

Two gateways live here and they look identical from the outside:

  DesktopQuickBooksGateway - the real thing, over pywin32 and the QuickBooks SDK.
  DryRunQuickBooksGateway  - a convincing fake that keeps records in memory.

Because they share a shape, the rest of the app never needs to know which one
it is holding, and dry-run mode exercises exactly the same code path.
"""

from __future__ import annotations

import os
from contextlib import AbstractContextManager
from dataclasses import dataclass
from hashlib import sha1
from pathlib import Path
from typing import Protocol
from xml.etree import ElementTree as ET

from .config import InvoiceKindConfig
from .models import InvoiceSpec, WorkRecord
from .qbxml import (
    QbResponse,
    QbxmlBuilder,
    parse_invoice_matches,
    parse_response,
)


class QuickBooksError(RuntimeError):
    """Anything that stopped us from completing a QuickBooks request."""


@dataclass(frozen=True, slots=True)
class CustomerEntity:
    list_id: str
    full_name: str
    edit_sequence: str = ""   # needed before QuickBooks will accept a change
    notes: str = ""           # the Note box on a customer or job


@dataclass(frozen=True, slots=True)
class InvoiceEntity:
    txn_id: str
    ref_number: str
    customer_list_id: str | None = None
    edit_sequence: str = ""   # needed before QuickBooks will accept a change
    line_id: str = ""         # the one premium line, so a change replaces it


class QuickBooksGateway(Protocol):
    def __enter__(self) -> "QuickBooksGateway": ...
    def __exit__(self, exc_type, exc, traceback) -> None: ...
    def current_company_file(self) -> str: ...
    def find_customer(self, full_name: str) -> CustomerEntity | None: ...
    def find_customer_by_id(self, list_id: str) -> CustomerEntity | None: ...
    def rename_job(self, record: WorkRecord, existing: CustomerEntity) -> CustomerEntity: ...
    def create_customer(self, record: WorkRecord) -> CustomerEntity: ...
    def update_customer(self, record: WorkRecord, existing: CustomerEntity) -> CustomerEntity: ...
    def create_job(self, record: WorkRecord, parent: CustomerEntity) -> CustomerEntity: ...
    def find_invoice(self, ref_number: str) -> InvoiceEntity | None: ...
    def create_invoice(self, record: WorkRecord, customer_list_id: str) -> InvoiceEntity: ...
    def find_policy_invoice(self, ref_number: str, kind: str) -> InvoiceEntity | None: ...
    def create_policy_invoice(
        self, record: WorkRecord, job_list_id: str, kind: str
    ) -> tuple[InvoiceEntity, list[str]]: ...
    def update_policy_invoice(
        self, record: WorkRecord, job_list_id: str, kind: str, existing: InvoiceEntity
    ) -> tuple[InvoiceEntity, list[str]]: ...
    def note_full_policy_number(self, record: WorkRecord, job: CustomerEntity) -> str: ...


class RequestProcessorTransport(AbstractContextManager["RequestProcessorTransport"]):
    """A thin pywin32 wrapper around the installed QuickBooks request processor.

    This is the four-line dance from the starter scripts - Dispatch,
    OpenConnection2, BeginSession, ProcessRequest - with cleanup that always
    runs and a check that the company file open in QuickBooks is the expected one.
    """

    LOCAL_QUICKBOOKS_DESKTOP = 1
    FILE_OPEN_DO_NOT_CARE = 2

    def __init__(self, company_file: str = "", app_name: str = "QB Automation Dashboard"):
        self.company_file = str(Path(company_file)) if company_file.strip() else ""
        self.app_name = app_name
        self._processor = None
        self._ticket = None
        self._pythoncom = None

    def __enter__(self) -> "RequestProcessorTransport":
        try:
            import pythoncom  # type: ignore
            import win32com.client  # type: ignore
        except ImportError as exc:
            raise QuickBooksError(
                "pywin32 is not installed. Run: pip install pywin32"
            ) from exc
        pythoncom.CoInitialize()
        self._pythoncom = pythoncom
        try:
            self._processor = win32com.client.Dispatch("QBXMLRP2.RequestProcessor")
            self._processor.OpenConnection2("", self.app_name, self.LOCAL_QUICKBOOKS_DESKTOP)
            self._ticket = self._begin_session()
            if self.company_file:
                actual = self.current_company_file()
                if _normal_path(actual) != _normal_path(self.company_file):
                    raise QuickBooksError(
                        f"Wrong QuickBooks company is open. Expected {self.company_file!r}; "
                        f"QuickBooks has {actual!r}."
                    )
            return self
        except Exception as exc:
            self.__exit__(None, None, None)
            if isinstance(exc, QuickBooksError):
                raise
            raise QuickBooksError(self._explain(exc)) from exc

    @staticmethod
    def _explain(exc: Exception) -> str:
        """Turn the SDK's COM error into something worth reading.

        The raw text is a nested tuple of HRESULTs quoting QuickBooks at the
        developer, not at whoever is sitting in front of it.
        """
        raw = str(exc)
        if "must include the name of the data file" in raw:
            return (
                "QuickBooks does not have a company file open. Start QuickBooks "
                "Desktop and open your company (File, Open Previous Company), "
                "then try again. If QuickBooks is already showing your company, "
                "check that it and this app were started by the same Windows "
                "user - running one of them as administrator and not the other "
                "hides the session from the app."
            )
        if "denied access" in raw or "not allowed" in raw.lower():
            return (
                "QuickBooks has this app on its refused list. In QuickBooks go to "
                "Edit, Preferences, Integrated Applications, Company Preferences, "
                "select 'QB Automation Dashboard' and press Remove. Then start the "
                "dashboard again and answer the permission box with 'Yes, whenever "
                "my QuickBooks company file is open'."
            )
        if "Class not registered" in raw or "Invalid class string" in raw:
            return (
                "The QuickBooks connector was not found on this computer. It is "
                "installed by QuickBooks Desktop itself, so this usually means "
                "QuickBooks Desktop is not installed here. (The Intuit SDK "
                "download is not what is needed - only QuickBooks itself.)"
            )
        return (
            "Could not open the QuickBooks Desktop SDK session. Check that QuickBooks "
            f"is running with a company file open, then approve the access request. ({exc})"
        )

    def _begin_session(self):
        """Attach to the company file QuickBooks already has open.

        Naming the file explicitly tells QuickBooks to *start* that file, which
        it refuses to do when it is already running with it open ("Could not
        start QuickBooks"). So we ask for whatever is open, and only name the
        file as a fallback - that path is what launches QuickBooks when it is
        closed. Either way the caller then checks it got the right company.
        """
        try:
            return self._processor.BeginSession("", self.FILE_OPEN_DO_NOT_CARE)
        except Exception:
            if not self.company_file:
                raise
            return self._processor.BeginSession(self.company_file, self.FILE_OPEN_DO_NOT_CARE)

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._processor is not None and self._ticket is not None:
            try:
                self._processor.EndSession(self._ticket)
            except Exception:
                pass
        if self._processor is not None:
            try:
                self._processor.CloseConnection()
            except Exception:
                pass
        self._ticket = None
        self._processor = None
        if self._pythoncom is not None:
            self._pythoncom.CoUninitialize()
            self._pythoncom = None

    def current_company_file(self) -> str:
        self._require_session()
        return str(self._processor.GetCurrentCompanyFileName(self._ticket))

    def request(self, qbxml: str) -> str:
        self._require_session()
        try:
            return str(self._processor.ProcessRequest(self._ticket, qbxml))
        except Exception as exc:
            raise QuickBooksError(f"QuickBooks rejected the request: {exc}") from exc

    def _require_session(self) -> None:
        if self._processor is None or self._ticket is None:
            raise QuickBooksError("The QuickBooks session is not open.")


class DesktopQuickBooksGateway(AbstractContextManager["DesktopQuickBooksGateway"]):
    def __init__(
        self,
        company_file: str = "",
        app_name: str = "QB Automation Dashboard",
        qbxml_version: str = "13.0",
        invoice_template: str = "",
        premium: InvoiceKindConfig | None = None,
        commission: InvoiceKindConfig | None = None,
        fees: InvoiceKindConfig | None = None,
    ):
        self.transport = RequestProcessorTransport(company_file, app_name)
        self.builder = QbxmlBuilder(qbxml_version)
        self.invoice_template = invoice_template
        self.premium = premium or InvoiceKindConfig("", "")
        self.commission = commission or InvoiceKindConfig("", "")
        self.fees = fees or InvoiceKindConfig("", "")
        # These lists are read once per session, not once per invoice.
        self._terms: dict[str, str] | None = None
        self._reps: dict[str, str] | None = None
        self._items: dict[str, str] | None = None

    def __enter__(self) -> "DesktopQuickBooksGateway":
        self.transport.__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.transport.__exit__(exc_type, exc, traceback)

    def current_company_file(self) -> str:
        return self.transport.current_company_file()

    def find_customer(self, full_name: str) -> CustomerEntity | None:
        response = self._query(self.builder.customer_query(full_name))
        if response is None or not response.list_id:
            return None
        return CustomerEntity(
            response.list_id,
            response.full_name or full_name,
            response.edit_sequence or "",
            response.notes or "",
        )

    def find_customer_by_id(self, list_id: str) -> CustomerEntity | None:
        response = self._query(self.builder.customer_query_by_id(list_id))
        if response is None or not response.list_id:
            return None
        return CustomerEntity(
            response.list_id,
            response.full_name or "",
            response.edit_sequence or "",
            response.notes or "",
        )

    def rename_job(self, record: WorkRecord, existing: CustomerEntity) -> CustomerEntity:
        if not existing.edit_sequence:
            raise QuickBooksError(
                "QuickBooks did not supply an EditSequence, so this job cannot be renamed."
            )
        response = self._send(
            self.builder.job_mod(record, existing.list_id, existing.edit_sequence)
        )
        return CustomerEntity(
            response.list_id or existing.list_id,
            response.full_name or record.quickbooks_job_full_name,
            response.edit_sequence or "",
        )

    def update_customer(self, record: WorkRecord, existing: CustomerEntity) -> CustomerEntity:
        if not existing.edit_sequence:
            raise QuickBooksError(
                "QuickBooks did not supply an EditSequence, so this customer cannot be updated."
            )
        response = self._send(
            self.builder.customer_mod(record, existing.list_id, existing.edit_sequence)
        )
        return CustomerEntity(
            response.list_id or existing.list_id,
            response.full_name or record.quickbooks_customer_name,
            response.edit_sequence or "",
        )

    def create_customer(self, record: WorkRecord) -> CustomerEntity:
        response = self._send(self.builder.customer_add(record))
        if not response.list_id:
            raise QuickBooksError("QuickBooks did not return a ListID for the new customer.")
        return CustomerEntity(response.list_id, response.full_name or record.quickbooks_customer_name)

    def create_job(self, record: WorkRecord, parent: CustomerEntity) -> CustomerEntity:
        response = self._send(self.builder.job_add(record, parent.list_id))
        if not response.list_id:
            raise QuickBooksError("QuickBooks did not return a ListID for the new job.")
        return CustomerEntity(response.list_id, response.full_name or record.quickbooks_job_full_name)

    def find_invoice(self, ref_number: str) -> InvoiceEntity | None:
        response = self._query(self.builder.invoice_query(ref_number))
        if response is None or not response.txn_id:
            return None
        return InvoiceEntity(
            response.txn_id,
            response.ref_number or ref_number,
            response.customer_list_id,
            response.edit_sequence or "",
            response.line_id or "",
        )

    def create_invoice(self, record: WorkRecord, customer_list_id: str) -> InvoiceEntity:
        response = self._send(
            self.builder.invoice_add(record, customer_list_id, self.invoice_template)
        )
        if not response.txn_id:
            raise QuickBooksError("QuickBooks did not return a TxnID for the new invoice.")
        return InvoiceEntity(
            response.txn_id,
            response.ref_number or record.invoice_number,
            response.customer_list_id or customer_list_id,
        )

    def find_policy_invoice(self, ref_number: str, kind: str) -> InvoiceEntity | None:
        """The invoice of this kind carrying this policy number, if any.

        A policy's premium and commission invoices share its number, so the
        A/R account is what tells them apart.
        """
        xml = self.transport.request(self.builder.invoice_query(ref_number))
        self._raise_on_error(xml)
        wanted = _fold(self.kind_config(kind).ar_account)
        for match in parse_invoice_matches(xml):
            if wanted and _fold(match["ar_account"]) != wanted:
                continue
            return InvoiceEntity(
                match["txn_id"],
                match["ref_number"] or ref_number,
                match["customer_list_id"] or None,
                match["edit_sequence"],
                match["line_id"],
            )
        return None

    def kind_config(self, kind: str):
        return {"commission": self.commission, "fees": self.fees}.get(kind, self.premium)

    def _spec(self, record: WorkRecord, kind: str, notes: list[str]) -> InvoiceSpec:
        """Turn the row into the values this kind of invoice carries."""
        settings = self.kind_config(kind)
        if kind == "commission":
            item = self._resolve_item(record.commission_item, settings, notes)
            description = record.commission_description
            amount = record.commission_amount
        elif kind == "fees":
            # Every fees invoice bills the same item, named in the settings.
            item = self._resolve_item(settings.item, settings, notes)
            description = record.fees_description
            amount = record.fees_amount
        else:
            item = record.premium_item
            description = record.premium_description
            amount = record.premium_amount
        return InvoiceSpec(
            kind=kind,
            item=item,
            description=description,
            amount=amount,
            class_name=record.premium_class,
            template=settings.template,
            ar_account=settings.ar_account,
            terms=self._resolve_terms(record.transaction_type, notes),
            sales_rep=self._resolve_sales_rep(record.lead_source, notes),
        )

    def create_policy_invoice(
        self, record: WorkRecord, job_list_id: str, kind: str
    ) -> tuple[InvoiceEntity, list[str]]:
        notes: list[str] = []
        spec = self._spec(record, kind, notes)
        response = self._send(self.builder.policy_invoice_add(record, spec, job_list_id))
        if not response.txn_id:
            raise QuickBooksError("QuickBooks did not return a TxnID for the new invoice.")
        return (
            InvoiceEntity(
                response.txn_id,
                response.ref_number or record.invoice_number,
                response.customer_list_id or job_list_id,
            ),
            notes,
        )

    def update_policy_invoice(
        self, record: WorkRecord, job_list_id: str, kind: str, existing: InvoiceEntity
    ) -> tuple[InvoiceEntity, list[str]]:
        """Bring an invoice already in QuickBooks back in line with the sheet."""
        if not existing.edit_sequence or not existing.line_id:
            raise QuickBooksError(
                "QuickBooks did not supply what is needed to change this invoice. "
                "Refresh the policies and try again."
            )
        notes: list[str] = []
        spec = self._spec(record, kind, notes)
        response = self._send(
            self.builder.policy_invoice_mod(
                record,
                spec,
                job_list_id,
                existing.txn_id,
                existing.edit_sequence,
                existing.line_id,
            )
        )
        return (
            InvoiceEntity(
                response.txn_id or existing.txn_id,
                response.ref_number or record.invoice_number,
                response.customer_list_id or job_list_id,
                response.edit_sequence or "",
                response.line_id or existing.line_id,
            ),
            notes,
        )

    def _resolve_item(self, name: str, settings, notes: list[str]) -> str:
        """Carriers arrive as free text, so missing ones become items."""
        if not name:
            return ""
        if self._items is None:
            xml = self.transport.request(self.builder.service_item_list())
            self._raise_on_error(xml)
            self._items = {
                value.casefold(): value
                for value in _texts(xml, "ItemServiceRet", "FullName")
            }
        existing = self._items.get(name.casefold())
        if existing:
            return existing
        if not settings.item_income_account:
            notes.append(
                f"Carrier '{name}' is not in the Item List and no income account is "
                "configured, so it could not be added."
            )
            return name
        short = name[:31]
        self._send(self.builder.service_item_add(short, settings.item_income_account))
        self._items[short.casefold()] = short
        notes.append(
            f"Added '{short}' to the QuickBooks Item List, posting to "
            f"'{settings.item_income_account}'."
        )
        return short

    def note_full_policy_number(self, record: WorkRecord, job: CustomerEntity) -> str:
        """Record the untruncated policy number in the job's Note box.

        QuickBooks caps an invoice number at 11 characters, so a long policy
        number loses its tail. The full one goes in the invoice Memo and here,
        where it is visible from the job itself. One job can carry several
        policies over the years, so the line is appended, never overwritten.
        """
        line = f"Policy #: {record.invoice_number}"
        existing = job.notes or ""
        if line in existing:
            return ""
        current = self.find_customer_by_id(job.list_id) or job
        if line in (current.notes or ""):
            return ""
        combined = f"{current.notes}\n{line}".strip() if current.notes else line
        self._send(
            self.builder.job_note_mod(current.list_id, current.edit_sequence, combined[:4095])
        )
        return f"Full policy number {record.invoice_number} saved in the job's Note."

    # --- the two renamed template fields ------------------------------------
    #
    # "Transaction Type" is really the Terms field and "Source" is really the
    # Rep field, so both point at a QuickBooks list. An invoice that names a
    # list entry that does not exist is rejected outright, so anything missing
    # is added first - and said out loud, never silently.

    def _resolve_terms(self, value: str, notes: list[str]) -> str:
        if not value:
            return ""
        if self._terms is None:
            xml = self.transport.request(self.builder.terms_list())
            self._raise_on_error(xml)
            self._terms = {
                name.casefold(): name for name in _texts(xml, "StandardTermsRet", "Name")
            }
        existing = self._terms.get(value.casefold())
        if existing:
            return existing
        name = value[:31]
        try:
            self._send(self.builder.terms_add(name))
        except QuickBooksError as exc:
            notes.append(f"Transaction Type '{value}' could not be added to the Terms list: {exc}")
            return ""
        self._terms[name.casefold()] = name
        notes.append(f"Added '{name}' to the QuickBooks Terms list (Transaction Type).")
        return name

    def _resolve_sales_rep(self, value: str, notes: list[str]) -> str:
        """Find - or create - the rep whose initials the invoice will carry."""
        if not value:
            return ""
        if self._reps is None:
            self._load_reps()
        found = self._reps.get(value.casefold())
        if found:
            return found
        initial = self._free_initial(value)
        try:
            # The rep needs a person behind it; an Other Name is the lightest.
            self._send(self.builder.other_name_add(value))
        except QuickBooksError:
            pass   # most likely it already exists, which is exactly what we want
        try:
            self._send(self.builder.sales_rep_add(initial, value))
        except QuickBooksError as exc:
            notes.append(f"Source '{value}' could not be added to the Rep list: {exc}")
            return ""
        self._reps[value.casefold()] = initial
        self._reps[initial.casefold()] = initial
        notes.append(f"Added rep '{initial}' for '{value}' to the QuickBooks Rep list (Source).")
        return initial

    def _load_reps(self) -> None:
        xml = self.transport.request(self.builder.sales_rep_list())
        self._raise_on_error(xml)
        self._reps = {}
        for node in ET.fromstring(xml).iter("SalesRepRet"):
            initial = (node.findtext("Initial") or "").strip()
            if not initial:
                continue
            entity = (node.findtext("SalesRepEntityRef/FullName") or "").strip()
            self._reps[initial.casefold()] = initial
            if entity:
                self._reps[entity.casefold()] = initial

    def _free_initial(self, value: str) -> str:
        """Initials are capped at 5 characters, so collisions are real."""
        letters = "".join(char for char in value.upper() if char.isalnum()) or "REP"
        candidate = letters[:5]
        if candidate.casefold() not in self._reps:
            return candidate
        for suffix in range(2, 100):
            tail = str(suffix)
            candidate = f"{letters[: 5 - len(tail)]}{tail}"
            if candidate.casefold() not in self._reps:
                return candidate
        raise QuickBooksError(f"Could not find free rep initials for '{value}'.")

    def names_in_quickbooks(self) -> dict[str, set[str]]:
        """Every name the invoices reference, so they can be checked up front.

        Read-only, and asked for in one session: cheaper than discovering a
        renamed template one failed invoice at a time.
        """
        lists = {
            "accounts": (self.builder.account_list(), "AccountRet", "FullName"),
            "templates": (self.builder.template_list(), "TemplateRet", "Name"),
            "items": (self.builder.service_item_list(), "ItemServiceRet", "FullName"),
            "classes": (self.builder.class_list(), "ClassRet", "FullName"),
            "terms": (self.builder.terms_list(), "StandardTermsRet", "Name"),
            "reps": (self.builder.sales_rep_list(), "SalesRepRet", "Initial"),
        }
        found: dict[str, set[str]] = {}
        for key, (request, node, child) in lists.items():
            xml = self.transport.request(request)
            self._raise_on_error(xml)
            found[key] = set(_texts(xml, node, child))
        return found

    def _raise_on_error(self, xml: str) -> None:
        """An empty list is fine; a genuine QuickBooks error is not."""
        responses = parse_response(xml)
        if responses and not responses[0].ok and not responses[0].not_found:
            raise QuickBooksError(
                f"QuickBooks error {responses[0].status_code}: {responses[0].status_message}"
            )

    def _first(self, qbxml: str) -> QbResponse:
        responses = parse_response(self.transport.request(qbxml))
        if not responses:
            raise QuickBooksError("QuickBooks returned an empty response.")
        return responses[0]

    def _send(self, qbxml: str) -> QbResponse:
        response = self._first(qbxml)
        if not response.ok:
            raise QuickBooksError(f"QuickBooks error {response.status_code}: {response.status_message}")
        return response

    def _query(self, qbxml: str) -> QbResponse | None:
        """Like _send, but "no such record" is an answer rather than a failure."""
        response = self._first(qbxml)
        if response.ok:
            return response
        if response.not_found:
            return None
        raise QuickBooksError(f"QuickBooks error {response.status_code}: {response.status_message}")


class DryRunQuickBooksGateway(AbstractContextManager["DryRunQuickBooksGateway"]):
    """Behaves like QuickBooks, writes nothing. Used until live writes are unlocked."""

    def __init__(self, company_file: str = ""):
        self.company_file = company_file or "(dry run - no company file)"
        self.customers: dict[str, CustomerEntity] = {}
        self.invoices: dict[str, InvoiceEntity] = {}

    def __enter__(self) -> "DryRunQuickBooksGateway":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def current_company_file(self) -> str:
        return self.company_file

    def find_customer(self, full_name: str) -> CustomerEntity | None:
        return self.customers.get(full_name.casefold())

    def create_customer(self, record: WorkRecord) -> CustomerEntity:
        entity = CustomerEntity(
            _fake_id("customer", record.quickbooks_customer_name), record.quickbooks_customer_name, "1"
        )
        self.customers[record.quickbooks_customer_name.casefold()] = entity
        return entity

    def update_customer(self, record: WorkRecord, existing: CustomerEntity) -> CustomerEntity:
        entity = CustomerEntity(existing.list_id, record.quickbooks_customer_name, "2")
        self.customers.pop(existing.full_name.casefold(), None)
        self.customers[record.quickbooks_customer_name.casefold()] = entity
        return entity

    def create_job(self, record: WorkRecord, parent: CustomerEntity) -> CustomerEntity:
        full_name = record.quickbooks_job_full_name
        entity = CustomerEntity(_fake_id("job", full_name), full_name, "1")
        self.customers[full_name.casefold()] = entity
        return entity

    def find_customer_by_id(self, list_id: str) -> CustomerEntity | None:
        return next(
            (entity for entity in self.customers.values() if entity.list_id == list_id), None
        )

    def rename_job(self, record: WorkRecord, existing: CustomerEntity) -> CustomerEntity:
        full_name = record.quickbooks_job_full_name
        entity = CustomerEntity(existing.list_id, full_name, "2")
        self.customers.pop(existing.full_name.casefold(), None)
        self.customers[full_name.casefold()] = entity
        return entity

    def find_invoice(self, ref_number: str) -> InvoiceEntity | None:
        return self.invoices.get(ref_number.casefold())

    def create_invoice(self, record: WorkRecord, customer_list_id: str) -> InvoiceEntity:
        entity = InvoiceEntity(
            _fake_id("invoice", record.invoice_number), record.invoice_number, customer_list_id
        )
        self.invoices[record.invoice_number.casefold()] = entity
        return entity

    def find_policy_invoice(self, ref_number: str, kind: str) -> InvoiceEntity | None:
        return self.invoices.get(f"{kind}|{ref_number.casefold()}")

    def create_policy_invoice(
        self, record: WorkRecord, job_list_id: str, kind: str
    ) -> tuple[InvoiceEntity, list[str]]:
        entity = InvoiceEntity(
            _fake_id(kind, record.invoice_number),
            record.invoice_number,
            job_list_id,
            "1",
            "LINE-1",
        )
        self.invoices[f"{kind}|{record.invoice_number.casefold()}"] = entity
        return entity, []

    def update_policy_invoice(
        self, record: WorkRecord, job_list_id: str, kind: str, existing: InvoiceEntity
    ) -> tuple[InvoiceEntity, list[str]]:
        entity = InvoiceEntity(
            existing.txn_id, record.invoice_number, job_list_id, "2", existing.line_id
        )
        self.invoices[f"{kind}|{record.invoice_number.casefold()}"] = entity
        return entity, []

    def note_full_policy_number(self, record: WorkRecord, job: CustomerEntity) -> str:
        return f"Full policy number {record.invoice_number} saved in the job's Note."


def quickbooks_windows() -> list[str]:
    """Every window title QuickBooks currently has open.

    Read straight from Windows rather than the SDK, because the SDK has no way
    to say what is on screen - and what is on screen is what decides whether a
    write will succeed.
    """
    try:
        import win32gui  # type: ignore
    except ImportError:
        return []

    titles: list[str] = []

    def collect(handle, _unused=None) -> None:
        try:
            text = win32gui.GetWindowText(handle)
            # Windows keeps handles around for things that are not on screen.
            # Only what is actually showing can block a send.
            if text and win32gui.IsWindowVisible(handle):
                titles.append(text)
        except Exception:
            return
        try:
            win32gui.EnumChildWindows(handle, collect, None)
        except Exception:
            pass   # a window can close while we walk it

    try:
        win32gui.EnumWindows(collect, None)
    except Exception:
        return []
    return titles


def ui_blockers() -> dict[str, object]:
    """What QuickBooks has on screen that would spoil a send.

    Two things matter. The Customer Centre holds the customer list open, and
    QuickBooks can crash outright when that list is written to underneath it.
    A modal dialog - a crash report, a prompt - freezes every request until
    somebody clicks it.
    """
    titles = quickbooks_windows()
    # QuickBooks names its main window "<Company>  - Intuit QuickBooks ...",
    # which is the only way to know what it has open without a session.
    main = next((t for t in titles if "- Intuit QuickBooks" in t), "")
    open_company = main.split("- Intuit QuickBooks")[0].strip() if main else ""
    centre = next(
        (t for t in titles if t.startswith(("Customer Center", "Customer Centre"))), ""
    )
    dialog = next(
        (t for t in titles
         if "Unrecoverable Error" in t
         # QuickBooks' own access request. Until it is answered every SDK call
         # waits, which looks exactly like the app having hung.
         or t.strip() in ("Application permission", "Application Certificate")
         or t.strip() in ("QuickBooks Desktop Information",)),
        "",
    )
    return {
        "checked": bool(titles),          # false when the window list is unreadable
        "open_company": open_company,
        "customer_centre": centre,
        "dialog": dialog,
        "safe_to_send": bool(titles) and not centre and not dialog,
    }


def current_company_file() -> str:
    """Open a short read-only session just to report which company file is open."""
    with RequestProcessorTransport("", "QB Automation Dashboard") as transport:
        return transport.current_company_file()


def company_files(extra: str = "") -> list[dict[str, str]]:
    """Every QuickBooks company file this machine can offer to work in.

    QuickBooks keeps company files in a small number of well-known folders, so
    they can be listed without the SDK and without QuickBooks running. `extra`
    adds the folder of the company already configured, which covers files kept
    somewhere of the user's own choosing.
    """
    folders: list[Path] = []
    public = os.environ.get("PUBLIC", r"C:\Users\Public")
    profile = os.environ.get("USERPROFILE", "")
    for base in (public, profile):
        if base:
            folders.append(Path(base) / "Documents" / "Intuit" / "QuickBooks" / "Company Files")
    if extra.strip():
        folders.append(Path(extra).parent)

    found: dict[str, dict[str, str]] = {}
    for folder in folders:
        try:
            if not folder.is_dir():
                continue
            # One level down as well: QuickBooks makes a per-company subfolder
            # for files restored from a backup.
            entries = list(folder.glob("*.qbw")) + list(folder.glob("*/*.qbw"))
        except OSError:
            continue
        for entry in entries:
            key = _normal_path(str(entry))
            found.setdefault(key, {"display_name": entry.stem, "file_path": str(entry)})
    return sorted(found.values(), key=lambda item: item["display_name"].casefold())


def sdk_registered() -> bool:
    """True when the QuickBooks SDK is installed on this machine."""
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"QBXMLRP2.RequestProcessor"):
            return True
    except OSError:
        return False


def _fold(value: str | None) -> str:
    return (value or "").strip().casefold()


def _texts(xml: str, node_tag: str, child_tag: str) -> list[str]:
    return [
        text.strip()
        for node in ET.fromstring(xml).iter(node_tag)
        if (text := node.findtext(child_tag) or "").strip()
    ]


def _fake_id(kind: str, value: str) -> str:
    return f"DRY-{kind[:3].upper()}-{sha1(value.encode('utf-8')).hexdigest()[:12].upper()}"


def _normal_path(value: str) -> str:
    return os.path.normcase(os.path.abspath(value.strip()))
