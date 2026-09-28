"""The decision maker: for one row, work out what QuickBooks is actually missing.

The rule is always "look before you write": find the customer before creating
one, find the invoice number before creating an invoice. That is what keeps a
second run from duplicating a first run's work.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from time import sleep

from .config import AppConfig
from .models import Action, Operation, WorkRecord
from .quickbooks import WARNING, QuickBooksError, QuickBooksGateway
from .storage import Store

FINISHED = {"completed", "already_exists"}

# How many times a dropped QuickBooks session may be reopened during one batch.
MAX_REOPENS = 3

# Phrases QuickBooks uses when the session itself has gone bad, rather than the
# record being wrong. These are worth retrying on a fresh session; nothing else is.
SESSION_ERRORS = ("ticket parameter is invalid", "session is not open", "ticket is invalid")

# QuickBooks locks a list while its own window is showing it - the Customer
# Centre open on screen is enough. The row is fine; the list is simply busy, so
# it is worth waiting a moment rather than reporting a failure.
BUSY_ERRORS = ("in use by another user", "try again later", "is locked")
MAX_BUSY_RETRIES = 4
BUSY_PAUSE_SECONDS = 2.0

# What differs between the kinds of policy invoice, for wording the result.
_INVOICE_DETAIL = {
    "premium": {
        "label": "Premium",
        "operation": Operation.PREMIUM_INVOICE,
        "amount": lambda record: record.premium_amount,
        "item": lambda record: record.premium_item,
    },
    "commission": {
        "label": "Commission",
        "operation": Operation.COMMISSION_INVOICE,
        "amount": lambda record: record.commission_amount,
        "item": lambda record: record.commission_item,
    },
    "fees": {
        "label": "Fees",
        "operation": Operation.FEES_INVOICE,
        "amount": lambda record: record.fees_amount,
        "item": lambda record: "Fees",
    },
}


@dataclass(frozen=True, slots=True)
class ProcessingResult:
    source_id: str
    label: str
    status: str
    message: str
    dry_run: bool
    steps: list[str] = field(default_factory=list)
    customer_list_id: str | None = None
    job_list_id: str | None = None
    invoice_txn_id: str | None = None
    invoice_ref_number: str | None = None
    operation: str = Operation.CREATE_INVOICE.value

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class Processor:
    def __init__(self, config: AppConfig, store: Store, gateway_factory):
        self.config = config
        self.store = store
        self.gateway_factory = gateway_factory

    @property
    def mode(self) -> str:
        return self.config.mode

    @property
    def dry_run(self) -> bool:
        return not self.config.live_writes_enabled

    def process_many(
        self,
        records: list[WorkRecord],
        operation: Operation = Operation.CREATE_INVOICE,
        on_result=None,
    ) -> list[ProcessingResult]:
        """Work through the records, reporting each one as it finishes.

        `on_result` is called the moment a row is decided, which is what lets
        the dashboard tick rows over one at a time instead of sitting still
        until the whole batch comes back.
        """
        results: dict[str, ProcessingResult] = {}
        pending: list[WorkRecord] = []

        def announce(result: ProcessingResult) -> None:
            if on_result is not None:
                try:
                    on_result(result)
                except Exception:
                    pass   # progress reporting must never break the work itself

        # Rows whose invoice of this kind has been marked as not wanted. Held
        # here rather than filtered in the page, so a bulk send cannot raise
        # one by going round the dashboard.
        skipped = self._marked_not_needed(records, operation)

        # Step 1: sort out everything that needs no QuickBooks session at all.
        for record in records:
            if record.source_id in skipped:
                results[record.source_id] = ProcessingResult(
                    source_id=record.source_id,
                    label=record.quickbooks_job_name,
                    status="not_needed",
                    message="Marked as not needed, so nothing was sent.",
                    dry_run=self.config.mode != "quickbooks",
                    operation=operation.value,
                )
                announce(results[record.source_id])
                continue
            # Creating a customer ignores invoice-only problems (bad date, missing
            # item, zero amount) but still needs the policy number - see
            # WorkRecord.customer_reasons for why.
            if operation is Operation.CREATE_INVOICE:
                reasons = record.review_reasons()
            elif operation.invoice_kind:
                reasons = record.invoice_reasons(operation.invoice_kind)
            else:
                reasons = record.customer_reasons()
            if reasons:
                results[record.source_id] = self._save(
                    record, "needs_review", " ".join(reasons), error=" ".join(reasons),
                    operation=operation,
                )
                announce(results[record.source_id])
                continue
            done = self._already_done(record, operation)
            if done is not None:
                results[record.source_id] = done
                announce(done)
                continue
            pending.append(record)

        # Step 2: work through the rest over a QuickBooks session.
        if pending:
            self._run_batch(pending, results, operation, announce)

        return [results[record.source_id] for record in records if record.source_id in results]

    def _marked_not_needed(
        self, records: list[WorkRecord], operation: Operation
    ) -> set[str]:
        """Which of these rows have this kind of invoice marked as not wanted."""
        kind = operation.invoice_kind
        if not kind:
            return set()
        held = self.store.invoice_states([r.source_id for r in records], self.config.mode)
        return {
            source_id
            for source_id, kinds in held.items()
            if (state := kinds.get(kind)) is not None and state.status == "not_needed"
        }

    def _run_batch(
        self,
        pending: list[WorkRecord],
        results: dict[str, ProcessingResult],
        operation: Operation = Operation.CREATE_INVOICE,
        announce=None,
    ) -> None:
        """Process the batch over one session, reopening it if QuickBooks drops us.

        QuickBooks sometimes invalidates a session mid-batch (the telltale is
        "The ticket parameter is invalid"). Without this, the first such error
        would poison every remaining row, so we reopen and retry that row.
        """
        report = announce if announce is not None else (lambda result: None)

        try:
            self._guard_writes()
        except QuickBooksError as exc:
            for record in pending:
                results[record.source_id] = self._save(
                    record, "failed", str(exc), error=str(exc), operation=operation
                )
                report(results[record.source_id])
            return

        remaining = list(pending)
        reopens = 0
        while remaining:
            try:
                with self.gateway_factory() as gateway:
                    while remaining:
                        record = remaining[0]
                        result = self._process_one(gateway, record, operation)
                        if _is_session_error(result) and reopens < MAX_REOPENS:
                            reopens += 1
                            break  # drop the dead session, reopen, retry this row
                        # A locked list is a "come back in a moment", not a
                        # refusal: someone has the Customer Centre open.
                        waits = 0
                        while _is_busy_error(result) and waits < MAX_BUSY_RETRIES:
                            waits += 1
                            sleep(BUSY_PAUSE_SECONDS)
                            result = self._process_one(gateway, record, operation)
                        if _is_busy_error(result):
                            # Still locked after waiting: say what to close,
                            # rather than repeating QuickBooks' own wording.
                            result = self._save(
                                record,
                                "failed",
                                "QuickBooks has the customer list locked. Close the "
                                "Customer Centre (and any open customer or job window) "
                                "in QuickBooks, then send again.",
                                steps=result.steps,
                                error=result.message,
                                operation=operation,
                            )
                        results[record.source_id] = result
                        report(result)
                        remaining.pop(0)
                    else:
                        return  # finished the batch cleanly
            except QuickBooksError as exc:
                for record in remaining:
                    results[record.source_id] = self._save(
                        record, "failed", str(exc), error=str(exc), operation=operation
                    )
                    report(results[record.source_id])
                return
            except Exception as exc:  # unexpected - still report it per record
                message = f"Unexpected error: {exc}"
                for record in remaining:
                    results[record.source_id] = self._save(
                        record, "failed", message, error=message, operation=operation
                    )
                    report(results[record.source_id])
                return

    # --- one record ----------------------------------------------------------

    def _process_one(
        self,
        gateway: QuickBooksGateway,
        record: WorkRecord,
        operation: Operation = Operation.CREATE_INVOICE,
    ) -> ProcessingResult:
        steps: list[str] = []
        try:
            # 1. The corporation / account name becomes the customer.
            customer = gateway.find_customer(record.quickbooks_customer_name)

            if operation is Operation.UPDATE_CUSTOMER:
                if customer is None:
                    return self._save(
                        record,
                        "needs_review",
                        f"'{record.customer_name}' is not in QuickBooks yet, so there is "
                        "nothing to update. Send it as a new customer instead.",
                        steps=steps,
                        operation=operation,
                    )
                updated = gateway.update_customer(record, customer)
                steps.append(f"Customer '{updated.full_name}' updated in QuickBooks.")
                job_list_id = self._sync_job(gateway, record, updated, steps)
                return self._save(
                    record,
                    "completed",
                    "Customer and policy job updated." if record.has_job else "Customer updated.",
                    steps=steps,
                    customer_list_id=updated.list_id,
                    job_list_id=job_list_id,
                    operation=operation,
                )

            name_shortened = record.customer_name_was_shortened or record.company_name_was_shortened
            if customer is None:
                customer = gateway.create_customer(record)
                steps.append(f"Customer '{customer.full_name}' created.")
                if name_shortened:
                    steps.append("The full customer name was saved in the QuickBooks Notes field.")
            else:
                steps.append(f"Customer '{customer.full_name}' already existed - reused.")
                if name_shortened:
                    customer = gateway.update_customer(record, customer)
                    steps.append("The full customer name was saved in the QuickBooks Notes field.")

            # 2. The policy becomes a job underneath that customer.
            target = customer
            job_list_id = None
            if record.has_job:
                job = gateway.find_customer(record.quickbooks_job_full_name)
                if job is None:
                    job = gateway.create_job(record, customer)
                    steps.append(f"Job '{job.full_name}' created.")
                else:
                    steps.append(f"Job '{job.full_name}' already existed - reused.")
                target = job
                job_list_id = job.list_id

            if operation.invoice_kind:
                # The customer and job genuinely exist now, so say so - otherwise
                # the row would still show its customer as pending.
                self.store.save(
                    record,
                    self.mode,
                    status="completed",
                    message="Customer and policy job are ready.",
                    customer_list_id=customer.list_id,
                    job_list_id=job_list_id,
                    operation=operation.value,
                    steps=list(steps),
                )
                return self._policy_invoice(
                    gateway, record, target, steps, operation.invoice_kind
                )

            if operation is Operation.CREATE_CUSTOMER or record.action is Action.CUSTOMER_ONLY:
                return self._save(
                    record,
                    "completed",
                    "Customer and policy job are ready." if record.has_job
                    else "Customer is ready in QuickBooks.",
                    steps=steps,
                    customer_list_id=customer.list_id,
                    job_list_id=job_list_id,
                    operation=operation,
                )

            # 3. The invoice is raised against the job when there is one.
            invoice = gateway.find_invoice(record.quickbooks_invoice_number)
            if invoice is None:
                invoice = gateway.create_invoice(record, target.list_id)
                steps.append(
                    f"Invoice {invoice.ref_number} created on '{target.full_name}' "
                    f"for {record.line_total:f}."
                )
                if record.invoice_number_was_shortened:
                    steps.append(
                        f"Policy number was shortened to {record.quickbooks_invoice_number} for "
                        "QuickBooks (11-character limit); the full number is in the invoice Memo."
                    )
                status = "completed"
                message = "Customer, job and invoice completed." if record.has_job else "Customer and invoice completed."
            else:
                if invoice.customer_list_id and invoice.customer_list_id != target.list_id:
                    raise QuickBooksError(
                        f"Policy number {record.invoice_number} already exists in QuickBooks "
                        "as an invoice for a different customer or job."
                    )
                steps.append(f"Invoice {invoice.ref_number} already existed - nothing was created.")
                status = "already_exists"
                message = "This invoice number is already in QuickBooks."

            return self._save(
                record,
                status,
                message,
                steps=steps,
                customer_list_id=customer.list_id,
                job_list_id=job_list_id,
                invoice_txn_id=invoice.txn_id,
                invoice_ref_number=invoice.ref_number,
                operation=operation,
            )
        except QuickBooksError as exc:
            return self._save(
                record, "failed", str(exc), steps=steps, error=str(exc), operation=operation
            )

    def _policy_invoice(
        self, gateway, record: WorkRecord, target, steps: list[str], kind: str
    ) -> ProcessingResult:
        """Raise one kind of invoice against the policy's job.

        A policy's premium and commission invoices share its policy number, so
        the lookup is by kind - otherwise one would be mistaken for the other
        and updated in its place.
        """
        spec = _INVOICE_DETAIL[kind]
        existing = gateway.find_policy_invoice(
            record.quickbooks_invoice_number,
            kind,
            job_list_id=target.list_id,
            policy_number=record.invoice_number,
        )
        if existing is not None and existing.customer_list_id == target.list_id:
            # Sending again is how an edited row is corrected, so the existing
            # invoice is brought up to date rather than left as it was.
            invoice, notes = gateway.update_policy_invoice(
                record, target.list_id, kind, existing
            )
            verb = "updated"
        else:
            invoice, notes = gateway.create_policy_invoice(record, target.list_id, kind)
            verb = "created"
        steps.append(
            f"{spec['label']} invoice {invoice.ref_number} {verb} on '{target.full_name}' "
            f"for {spec['amount'](record):f}, item '{spec['item'](record)}', "
            f"class '{record.premium_class}'."
        )
        if record.invoice_number_was_shortened:
            steps.append(
                f"Policy number was shortened to {record.quickbooks_invoice_number} for "
                "QuickBooks (11-character limit); the full number is in the invoice Memo."
            )
            # The Memo is on the invoice; the Note is on the job, where it can
            # be seen without opening a transaction.
            note = gateway.note_full_policy_number(record, target)
            if note:
                steps.append(note)
        steps.extend(notes)
        # A warning goes in the message itself, not only the detail log: the
        # invoice was made, but something on it is not what the row asked for,
        # and a bare "invoice created" would say everything was fine.
        warnings = [note.removeprefix(WARNING) for note in notes if note.startswith(WARNING)]
        message = f"{spec['label']} invoice {verb}."
        if warnings:
            message = f"{message} {' '.join(warnings)}"
        return self._save(
            record,
            "completed",
            message,
            steps=steps,
            invoice_txn_id=invoice.txn_id,
            invoice_ref_number=invoice.ref_number,
            operation=spec["operation"],
        )

    def _sync_job(self, gateway, record: WorkRecord, customer, steps: list[str]) -> str | None:
        """Make the policy job in QuickBooks match the Line of Business we hold.

        If the job already exists under the expected name there is nothing to
        do. If it does not, but we created one for this row previously, the
        Line of Business was edited - so rename that job rather than leaving
        the old one orphaned and adding a duplicate beside it.
        """
        if not record.has_job:
            return None

        job = gateway.find_customer(record.quickbooks_job_full_name)
        if job is not None:
            steps.append(f"Job '{job.full_name}' already matches - nothing to change.")
            return job.list_id

        state = self.store.get(record.source_id, self.mode)
        previous = (
            gateway.find_customer_by_id(state.job_list_id)
            if state is not None and state.job_list_id
            else None
        )
        if previous is not None:
            renamed = gateway.rename_job(record, previous)
            steps.append(f"Job renamed from '{previous.full_name}' to '{renamed.full_name}'.")
            return renamed.list_id

        created = gateway.create_job(record, customer)
        steps.append(f"Job '{created.full_name}' created.")
        return created.list_id

    def _already_done(
        self, record: WorkRecord, operation: Operation = Operation.CREATE_INVOICE
    ) -> ProcessingResult | None:
        """Has this exact row already been finished in this mode before?"""
        if operation is Operation.UPDATE_CUSTOMER:
            return None  # an update is always worth re-sending
        if operation.invoice_kind:
            # Also always worth re-sending: an invoice already in QuickBooks is
            # updated in place, which is how a corrected row gets fixed.
            return None
        state = self.store.get(record.source_id, self.mode)
        if operation is Operation.PREMIUM_INVOICE:
            if state is None or state.premium_status not in FINISHED or not state.premium_txn_id:
                return None
            return ProcessingResult(
                source_id=record.source_id,
                label=_label(record),
                status="already_exists",
                message="This premium invoice was already created on an earlier run.",
                dry_run=self.dry_run,
                steps=["Skipped - already recorded as done in the local history."],
                invoice_txn_id=state.premium_txn_id,
                invoice_ref_number=state.premium_ref_number,
                operation=operation.value,
            )
        if state is None or state.status not in FINISHED:
            return None
        if operation is Operation.CREATE_CUSTOMER:
            # Only the customer matters here, so an invoice-less run still counts.
            return None if not state.customer_list_id else self._skipped(record, state)
        if record.action is Action.CUSTOMER_AND_INVOICE and not state.invoice_txn_id:
            return None
        if record.action is Action.CUSTOMER_ONLY and not state.customer_list_id:
            return None
        return self._skipped(record, state)

    def _skipped(self, record: WorkRecord, state) -> ProcessingResult:
        return ProcessingResult(
            source_id=record.source_id,
            label=_label(record),
            status="already_exists",
            message="This policy was already processed on an earlier run.",
            dry_run=self.dry_run,
            steps=["Skipped - already recorded as done in the local history."],
            customer_list_id=state.customer_list_id,
            invoice_txn_id=state.invoice_txn_id,
            invoice_ref_number=state.invoice_ref_number,
        )

    def _guard_writes(self) -> None:
        """Refuse to open a live session unless every safety switch is on."""
        if self.config.mode != "quickbooks":
            return
        if not self.config.allow_writes:
            raise QuickBooksError(
                "Mode is 'quickbooks' but allow_writes is still false in config.json."
            )
        if not self.config.company.file_path.strip():
            raise QuickBooksError(
                "Live writes need company.file_path in config.json so the app can confirm "
                "the right company file is open. Run: python -m qb_agent whoami"
            )

    def _save(
        self,
        record: WorkRecord,
        status: str,
        message: str,
        *,
        steps: list[str] | None = None,
        customer_list_id: str | None = None,
        job_list_id: str | None = None,
        invoice_txn_id: str | None = None,
        invoice_ref_number: str | None = None,
        error: str | None = None,
        operation: Operation = Operation.CREATE_INVOICE,
    ) -> ProcessingResult:
        if operation.invoice_kind:
            # Kept apart so raising an invoice never rewrites the customer's status.
            self.store.save_invoice(
                record,
                self.mode,
                operation.invoice_kind,
                status=status,
                message=message,
                txn_id=invoice_txn_id,
                ref_number=invoice_ref_number,
                error_message=error,
                steps=steps,
            )
        else:
            self.store.save(
                record,
                self.mode,
                status=status,
                message=message,
                customer_list_id=customer_list_id,
                job_list_id=job_list_id,
                invoice_txn_id=invoice_txn_id,
                invoice_ref_number=invoice_ref_number,
                error_message=error,
                operation=operation.value,
                steps=steps,
            )
        return ProcessingResult(
            source_id=record.source_id,
            label=_label(record),
            status=status,
            message=message,
            dry_run=self.dry_run,
            steps=steps or [],
            customer_list_id=customer_list_id,
            job_list_id=job_list_id,
            invoice_txn_id=invoice_txn_id,
            invoice_ref_number=invoice_ref_number,
            operation=operation.value,
        )


def _is_busy_error(result: ProcessingResult) -> bool:
    """QuickBooks has the list open in its own window; waiting usually clears it."""
    if result.status != "failed":
        return False
    message = result.message.casefold()
    return any(phrase in message for phrase in BUSY_ERRORS)


def _is_session_error(result: ProcessingResult) -> bool:
    if result.status != "failed":
        return False
    message = result.message.casefold()
    return any(phrase in message for phrase in SESSION_ERRORS)


def _label(record: WorkRecord) -> str:
    who = record.job_full_name or f"Row {record.row_number}"
    return f"{who} - invoice {record.invoice_number}" if record.invoice_number else who
