"""The decision maker: for one row, work out what QuickBooks is actually missing.

The rule is always "look before you write": find the customer before creating
one, find the invoice number before creating an invoice. That is what keeps a
second run from duplicating a first run's work.
"""

from __future__ import annotations

import re
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

# QuickBooks Desktop is sensitive to sustained SDK traffic. A completed row is
# committed before the session is closed, so deliberately taking a short break
# and opening a fresh SDK session cannot duplicate work but gives Desktop time
# to catch up.
ROWS_PER_QUICKBOOKS_SESSION = 8
QUICKBOOKS_SESSION_PAUSE_SECONDS = 4.0

# Common Line of Business abbreviations already used in QuickBooks. The
# fallback below also understands consonant abbreviations such as prpty and
# liab, but these explicit forms keep the most frequent cases unambiguous.
_LOB_WORDS = {
    "comm": "commercial",
    "prop": "property",
    "prpty": "property",
    "propty": "property",
    "ppty": "property",
    "proprty": "property",
    "gen": "general",
    "genl": "general",
    "liab": "liability",
    "pkg": "package",
    "prof": "professional",
    "empl": "employment",
    "wrks": "workers",
    "work": "workers",
    "comp": "compensation",
}

# Exact Line of Business equivalents that a word-by-word abbreviation rule
# cannot safely infer, because the two spellings are not the same number of
# words: "Prpty" and "Commercial Property" mean one thing and look like two.
# Every value is the single canonical meaning for that group, making this
# deliberately symmetric: either spelling can already be in QuickBooks and
# the other can arrive from the dashboard.
#
# Read out of the company file itself rather than imagined - 6,963 jobs, 439
# distinct spellings - so what is here is what people have actually typed,
# misspellings and all. A spelling that is genuinely a different cover is
# deliberately absent: Homeowners (Condo), Umbrella - Personal, the numbered
# Property 1 / Flood 2 and so on stay the separate jobs they are.
_LOB_EQUIVALENTS = {
    # Business Owners Policy / package.
    "bop": "package",
    "business owners policy": "package",
    "business owner policy": "package",
    "package": "package",
    "pkg": "package",
    "pakage": "package",
    # Property. Plain Property and Commercial Property are one cover here, so
    # the one-word spellings carry across to the two-word ones.
    "property": "commercial property",
    "properties": "commercial property",
    "prpty": "commercial property",
    "propty": "commercial property",
    "prop": "commercial property",
    "ppty": "commercial property",
    "proprty": "commercial property",
    "proprety": "commercial property",
    "propert": "commercial property",
    "commercial property": "commercial property",
    "commercial prpty": "commercial property",
    "comm property": "commercial property",
    "comm prpty": "commercial property",
    "com prpty": "commercial property",
    "comp prpty": "commercial property",
    "comm prprt": "commercial property",
    "commerical property": "commercial property",
    # Liquor liability, including LL and the Liqour misspelling.
    "ll": "liquor liability",
    "liq": "liquor liability",
    "liquor": "liquor liability",
    "liquor liability": "liquor liability",
    "liquor liab": "liquor liability",
    "liqour liability": "liquor liability",
    "liqour liab": "liquor liability",
    "liqour liabilty": "liquor liability",
    # Commercial and personal motor are different covers and must not be run
    # together. Plain "Auto" is the personal one.
    "commercial auto": "commercial auto",
    "comm auto": "commercial auto",
    "com auto": "commercial auto",
    "auto commercial": "commercial auto",
    "auto comm": "commercial auto",
    "auto cl": "commercial auto",
    "auto": "personal auto",
    "autop": "personal auto",
    "personal auto": "personal auto",
    "auto personal": "personal auto",
    "auto p": "personal auto",
    "auto prsnl": "personal auto",
    # Homeowners. The bracketed forms - (Condo), (Renters) - are left out on
    # purpose: they are separate covers, not spellings of this one.
    "home": "homeowners",
    "homeowner": "homeowners",
    "homeowners": "homeowners",
    "homeoners": "homeowners",
    "home owner": "homeowners",
    "home owners": "homeowners",
    # Umbrella cover is often recorded as excess liability.
    "umbrella": "umbrella",
    "umberlla": "umbrella",
    "umb": "umbrella",
    "umbrella comm": "umbrella",
    "umb comm": "umbrella",
    "umberlla comm": "umbrella",
    "commercial umbrella": "umbrella",
    "commercial umb": "umbrella",
    "umbrella commercial": "umbrella",
    "excess liability": "umbrella",
    "excess liability umbrella": "umbrella",
    # Firearms / active-assailant cover. Includes the spellings reported from
    # the sheet so a typo does not create a second QuickBooks job.
    "firearms": "active assailant",
    "fire arms": "active assailant",
    "fire arm": "active assailant",
    "firms arms": "active assailant",
    "firm arm liability": "active assailant",
    "fire arms liability": "active assailant",
    "fire arms liab": "active assailant",
    "firearms liability": "active assailant",
    "active assailant": "active assailant",
    "assailant": "active assailant",
    "active assualat": "active assailant",
    "active assault": "active assailant",
    "shooting": "active assailant",
    "shooting liability": "active assailant",
    # Equipment breakdown, which is abbreviated several ways. Plain
    # "Equipment" is left alone - it is not clear it means this one.
    "eb": "equipment breakdown",
    "eq": "equipment breakdown",
    "eqb": "equipment breakdown",
    "eq breakdown": "equipment breakdown",
    "equipment breakdown": "equipment breakdown",
    "equipement breakdown": "equipment breakdown",
    # Employment practices. "Accidental medical coverage" is included here
    # because it is the agency's specified QuickBooks equivalent.
    "epli": "employment practices liability",
    "employment practices liability": "employment practices liability",
    "employment practices liability insurance": "employment practices liability",
    "empl practices liab": "employment practices liability",
    "empl practice liability": "employment practices liability",
    "empl practices liability": "employment practices liability",
    "accidental medical coverage": "employment practices liability",
    "accedental medical coverage": "employment practices liability",
    # Workers compensation.
    "wc": "workers compensation",
    "workers compensation": "workers compensation",
    "worker compensation": "workers compensation",
    "workers comp": "workers compensation",
    "worker comp": "workers compensation",
    "wokrer comp": "workers compensation",
    "work comp": "workers compensation",
    "worker com": "workers compensation",
    "worker comps": "workers compensation",
    "worker s compensation": "workers compensation",
    # Bonds. Named bonds - Performance, Lottery, Contractor - are their own
    # jobs and are not folded in here.
    "surety bond": "bond",
    "surey bond": "bond",
    "surety": "bond",
    "bond": "bond",
    # Builder's risk, whose apostrophe makes a word of its own once stripped.
    "builder s risk": "builders risk",
    "builders risk": "builders risk",
    "builder risk": "builders risk",
    "builers risk": "builders risk",
    # Pollution.
    "pollution": "pollution liability",
    "pollution liability": "pollution liability",
    "pollution liab": "pollution liability",
    # Widely used insurance abbreviations that are the same coverage name.
    "gl": "general liability",
    "cgl": "general liability",
    "genl": "general liability",
    "general liability": "general liability",
    "genl liability": "general liability",
    "general liab": "general liability",
    "genl liab": "general liability",
    "commercial general liability": "general liability",
    "d o": "directors officers",
    "directors officers": "directors officers",
    "directors and officers": "directors officers",
    "e o": "professional liability",
    "errors omissions": "professional liability",
    "errors and omissions": "professional liability",
    "professional liability": "professional liability",
    "professional liab": "professional liability",
    "cyber": "cyber liability",
    "cyber liability": "cyber liability",
}


def _lob_phrase(value: str) -> str:
    """A punctuation-insensitive LOB phrase, for exact business aliases."""
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _canonical_lob(value: str) -> str:
    phrase = _lob_phrase(value)
    return _LOB_EQUIVALENTS.get(phrase, phrase)


def _lob_tokens(value: str) -> list[str]:
    return [
        _LOB_WORDS.get(token, token)
        for token in re.findall(r"[a-z0-9]+", value.casefold())
    ]


def _lob_word_matches(left: str, right: str) -> bool:
    if left == right:
        return True
    short, long = sorted((left, right), key=len)
    if len(short) < 3 or len(short) >= len(long) or short[0] != long[0]:
        return False
    # QuickBooks users commonly omit vowels/letters: genl/general,
    # prpty/property, liab/liability. Requiring every abbreviation letter in
    # order is intentionally conservative; a row is reused only if the whole
    # phrase matches and only one job qualifies.
    position = 0
    for character in long:
        if position < len(short) and short[position] == character:
            position += 1
    return position == len(short)


def _lob_names_match(left: str, right: str) -> bool:
    left_canonical = _canonical_lob(left)
    right_canonical = _canonical_lob(right)
    if left_canonical == right_canonical:
        return bool(left_canonical)
    left_tokens = _lob_tokens(left_canonical)
    right_tokens = _lob_tokens(right_canonical)
    return (
        bool(left_tokens)
        and len(left_tokens) == len(right_tokens)
        and all(
            _lob_word_matches(left_word, right_word)
            for left_word, right_word in zip(left_tokens, right_tokens)
        )
    )

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
    # What QuickBooks calls them, carried back so the sheet can record the
    # destination in words as well as in IDs. Blank when the step did not
    # touch that entity.
    customer_name: str = ""
    job_name: str = ""
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
            elif operation is Operation.CREATE_BILL:
                customer_id, job_id = self._bill_mapping_ids(record)
                reasons = record.bill_reasons(customer_id, job_id)
                if not self._premium_was_created(record):
                    reasons.append("Create the premium invoice before creating its bill.")
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
        """Which of these rows have this kind of invoice marked as not wanted.

        Either because somebody said so, or because the row answers it itself:
        a direct-billed policy raises no premium, the carrier having billed
        the insured. Asked for by name - "Need it after all" - it is wanted
        again, and that mark is what decides.
        """
        kind = operation.invoice_kind
        if not kind:
            return set()
        held = self.store.invoice_states([r.source_id for r in records], self.config.mode)

        def skip(record: WorkRecord) -> bool:
            state = held.get(record.source_id, {}).get(kind)
            if state is not None and state.status:
                return state.status == "not_needed"
            # Only the direct-billing rule is applied here. A commission or
            # fees row with nothing to bill is left to fail its own checks and
            # say why, which is what somebody asking for it by name needs to
            # hear - silently calling it "not needed" would answer a question
            # they did not ask.
            return kind == "premium" and record.direct_billed

        return {record.source_id for record in records if skip(record)}

    def _premium_was_created(self, record: WorkRecord) -> bool:
        saved = self.store.invoice_states([record.source_id], self.mode)
        premium = saved.get(record.source_id, {}).get("premium")
        if premium is not None and premium.status in FINISHED and premium.txn_id:
            return True
        sheet_status = record._extra("qb_premium").strip().casefold()
        return sheet_status in {"created", "already there"}

    def _bill_mapping_ids(self, record: WorkRecord) -> tuple[str, str]:
        saved = self.store.get(record.source_id, self.mode)
        customer_id = record.mapped_customer_list_id or (saved.customer_list_id if saved else "")
        job_id = record.mapped_job_list_id or (saved.job_list_id if saved else "")
        return customer_id or "", job_id or ""

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
                    rows_this_session = 0
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
                        rows_this_session += 1
                        if (
                            self.config.live_writes_enabled
                            and rows_this_session >= ROWS_PER_QUICKBOOKS_SESSION
                            and remaining
                        ):
                            sleep(QUICKBOOKS_SESSION_PAUSE_SECONDS)
                            break  # close the SDK session; reopen for the next group
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
            if operation is Operation.CREATE_BILL:
                return self._create_bill(gateway, record, steps)

            # 1. The corporation / account name becomes the customer.
            mapped = bool(record.mapped_customer_list_id)
            customer = (
                gateway.find_customer_by_id(record.mapped_customer_list_id)
                if mapped
                else gateway.find_customer(record.quickbooks_customer_name)
            )

            if mapped and customer is None:
                return self._save(
                    record,
                    "needs_review",
                    "The selected QuickBooks customer no longer exists. Choose the location again.",
                    steps=steps,
                    operation=operation,
                )

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
                if mapped and customer.full_name.casefold() != record.quickbooks_customer_name.casefold():
                    return self._save(
                        record,
                        "needs_review",
                        "This policy is linked to an existing location customer. Its name was not "
                        "changed; edit that customer directly in QuickBooks if it needs renaming.",
                        steps=steps,
                        customer_list_id=customer.list_id,
                        operation=operation,
                    )
                updated = gateway.update_customer(record, customer)
                steps.append(f"Customer '{updated.full_name}' updated in QuickBooks.")
                job_list_id, job_problem = self._sync_job(gateway, record, updated, steps)
                if job_problem:
                    return self._save(
                        record,
                        "needs_review",
                        job_problem,
                        steps=steps,
                        customer_list_id=updated.list_id,
                        operation=operation,
                    )
                return self._save(
                    record,
                    "completed",
                    "Customer and policy job updated." if record.has_job else "Customer updated.",
                    steps=steps,
                    customer_list_id=updated.list_id,
                    job_list_id=job_list_id,
                    customer_name=updated.full_name,
                    operation=operation,
                )

            name_shortened = record.customer_name_was_shortened or record.company_name_was_shortened
            if customer is None:
                if self.config.mode == "quickbooks" and not record.create_new_customer_selected:
                    suggestions = gateway.customer_candidates(record.quickbooks_customer_name)
                    detail = (
                        " Similar QuickBooks customers were found."
                        if suggestions
                        else " No matching QuickBooks customer was found."
                    )
                    return self._save(
                        record,
                        "needs_review",
                        "Choose the correct QuickBooks location before sending this policy."
                        + detail,
                        steps=steps,
                        operation=operation,
                    )
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
                job, matches = self._find_lob_job(
                    gateway, customer, record.quickbooks_job_name, record.mapped_job_list_id
                )
                if len(matches) > 1:
                    names = ", ".join(f"'{item.full_name}'" for item in matches)
                    return self._save(
                        record,
                        "needs_review",
                        f"More than one QuickBooks job matches Line of Business "
                        f"'{record.quickbooks_job_name}': {names}. Choose or rename the job "
                        "in QuickBooks; no new job was created.",
                        steps=steps,
                        customer_list_id=customer.list_id,
                        operation=operation,
                    )
                if job is None:
                    job = gateway.create_job(record, customer)
                    steps.append(f"Job '{job.full_name}' created.")
                elif job.full_name.casefold() == (
                    f"{customer.full_name}:{record.quickbooks_job_name}".casefold()
                ):
                    steps.append(f"Job '{job.full_name}' already existed - reused.")
                else:
                    steps.append(
                        f"Job '{job.full_name}' matches dashboard Line of Business "
                        f"'{record.quickbooks_job_name}' - reused."
                    )
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
                    customer_name=customer.full_name,
                    job_name=target.full_name if record.has_job else "",
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
                customer_name=customer.full_name,
                job_name=target.full_name if record.has_job else "",
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

        The effective date goes with it. Some accounts keep one policy number
        across renewals, and by the number alone last year's invoice answers
        to this year's row: a 2026 renewal was reopening the 2025 invoice and
        overwriting it. An invoice has to fall in the year being sent before
        it counts as this policy's.
        """
        spec = _INVOICE_DETAIL[kind]
        existing = gateway.find_policy_invoice(
            record.quickbooks_invoice_number,
            kind,
            job_list_id=target.list_id,
            policy_number=record.invoice_number,
            policy_year=record.invoice_date.year if record.invoice_date else 0,
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
            # The job the invoice was raised against, so sending an invoice
            # records the destination in the sheet just as sending the
            # customer does. Only a job carries a colon in its name.
            job_list_id=target.list_id if ":" in target.full_name else None,
            job_name=target.full_name if ":" in target.full_name else "",
            invoice_txn_id=invoice.txn_id,
            invoice_ref_number=invoice.ref_number,
            operation=spec["operation"],
        )

    def _create_bill(self, gateway, record: WorkRecord, steps: list[str]) -> ProcessingResult:
        """Create one carrier bill against the exact customer/job already chosen."""
        try:
            customer_id, job_id = self._bill_mapping_ids(record)
            year = record.invoice_date.year if record.invoice_date else 0

            if self.dry_run:
                premium = None
            else:
                customer = gateway.find_customer_by_id(customer_id)
                job = gateway.find_customer_by_id(job_id)
                if customer is None or job is None:
                    raise QuickBooksError(
                        "The saved QuickBooks customer/job could not be found. "
                        "Choose the customer and job again before creating the bill."
                    )
                if job.parent_list_id and job.parent_list_id != customer.list_id:
                    raise QuickBooksError(
                        "The saved job no longer belongs to the selected customer; "
                        "the bill was not created."
                    )
                if not job.parent_list_id and not job.full_name.casefold().startswith(
                    customer.full_name.casefold() + ":"
                ):
                    raise QuickBooksError(
                        "The saved job does not belong to the selected customer; "
                        "the bill was not created."
                    )
                premium = gateway.find_policy_invoice(
                    record.quickbooks_invoice_number,
                    "premium",
                    job_list_id=job_id,
                    policy_number=record.invoice_number,
                    policy_year=year,
                )
                if premium is None or premium.customer_list_id != job_id:
                    raise QuickBooksError(
                        "The premium invoice for this policy was not found on the saved "
                        "QuickBooks job. Create it first; no bill was created."
                    )
                steps.append(f"Premium invoice found on '{job.full_name}'.")

            vendor = gateway.resolve_bill_vendor(record.commission_item)
            if vendor.full_name.casefold() != record.commission_item.casefold():
                steps.append(
                    f"Carrier '{record.commission_item}' matched QuickBooks vendor "
                    f"'{vendor.full_name}'."
                )
            # Match the Premium invoice's source memo: the sheet's Prem inv
            # field (with the same fallback used by premium invoices).
            memo = record.premium_description
            existing = gateway.find_bill(
                record.quickbooks_bill_ref_number,
                vendor.list_id,
                job_id,
                record.invoice_date.isoformat() if record.invoice_date else "",
            )
            if existing is not None:
                if existing.job_list_id != job_id:
                    raise QuickBooksError(
                        f"A bill with this policy number already exists for vendor "
                        f"'{vendor.full_name}' on a different QuickBooks job "
                        "in this policy year; no duplicate bill was created."
                    )
                if existing.memo != memo:
                    bill = gateway.update_bill_memo(existing, memo)
                    steps.append(
                        "Existing bill Memo updated from Prem inv; vendor, date, "
                        "amount, account, class, and expense line were left unchanged."
                    )
                    return self._save(
                        record,
                        "completed",
                        "Existing carrier bill Memo updated from Prem inv.",
                        steps=steps,
                        customer_list_id=customer_id,
                        job_list_id=job_id,
                        invoice_txn_id=bill.txn_id,
                        invoice_ref_number=bill.ref_number,
                        operation=Operation.CREATE_BILL,
                    )
                steps.append(
                    f"Bill {existing.ref_number} already exists for vendor "
                    f"'{vendor.full_name}' on the selected job."
                )
                return self._save(
                    record,
                    "already_exists",
                    "This carrier bill already exists in QuickBooks; nothing new was created.",
                    steps=steps,
                    customer_list_id=customer_id,
                    job_list_id=job_id,
                    invoice_txn_id=existing.txn_id,
                    invoice_ref_number=existing.ref_number,
                    operation=Operation.CREATE_BILL,
                )

            bill = gateway.create_bill(record, vendor, job_id, memo)
            steps.append(
                "Full Payment is assigned to the bill expense line. QuickBooks Desktop's "
                "SDK does not expose the separate bill-header Class field."
            )
            steps.append(
                f"Bill {bill.ref_number} created for vendor '{vendor.full_name}' "
                f"for {record.premium_amount:f} on the selected job."
            )
            return self._save(
                record,
                "completed",
                "Carrier bill created.",
                steps=steps,
                customer_list_id=customer_id,
                job_list_id=job_id,
                invoice_txn_id=bill.txn_id,
                invoice_ref_number=bill.ref_number,
                operation=Operation.CREATE_BILL,
            )
        except QuickBooksError as exc:
            return self._save(
                record, "failed", str(exc), steps=steps, error=str(exc),
                operation=Operation.CREATE_BILL,
            )

    def _find_lob_job(self, gateway, customer, lob: str, known_list_id: str = ""):
        """Find one existing job equivalent to the dashboard LOB, never by policy.

        A job this row has already been sent to is taken by its ID, which no
        amount of renaming or re-spelling can disturb. Matching the line of
        business by name is what is left when nothing was recorded, or when
        the recorded job has since been deleted or moved to another customer.
        """
        if known_list_id:
            known = gateway.find_customer_by_id(known_list_id)
            if known is not None and known.full_name.startswith(f"{customer.full_name}:"):
                return known, [known]

        exact = gateway.find_customer(f"{customer.full_name}:{lob}")
        if exact is not None:
            return exact, [exact]

        matches = [
            job for job in gateway.customer_jobs(customer)
            if _lob_names_match(lob, job.full_name.rsplit(":", 1)[-1])
        ]
        return (matches[0] if len(matches) == 1 else None), matches

    def _sync_job(
        self, gateway, record: WorkRecord, customer, steps: list[str]
    ) -> tuple[str | None, str | None]:
        """Make the policy job in QuickBooks match the Line of Business we hold.

        If the job already exists under the expected name there is nothing to
        do. If it does not, but we created one for this row previously, the
        Line of Business was edited - so rename that job rather than leaving
        the old one orphaned and adding a duplicate beside it.
        """
        if not record.has_job:
            return None, None

        # No known ID is passed here. This is the path that renames a job when
        # the Line of Business was edited, and a job taken by its ID always
        # "matches" - which would skip the very rename being asked for.
        job, matches = self._find_lob_job(gateway, customer, record.quickbooks_job_name)
        if len(matches) > 1:
            names = ", ".join(f"'{item.full_name}'" for item in matches)
            return None, (
                f"More than one QuickBooks job matches Line of Business "
                f"'{record.quickbooks_job_name}': {names}. No job was changed."
            )
        if job is not None:
            if job.full_name.casefold() == (
                f"{customer.full_name}:{record.quickbooks_job_name}".casefold()
            ):
                steps.append(f"Job '{job.full_name}' already matches - nothing to change.")
            else:
                steps.append(
                    f"Job '{job.full_name}' matches dashboard Line of Business "
                    f"'{record.quickbooks_job_name}' - nothing to change."
                )
            return job.list_id, None

        # The sheet is asked before this machine's database: it carries what
        # every computer did, so a job created on one machine is still renamed
        # rather than duplicated when the row is edited on another.
        state = self.store.get(record.source_id, self.mode)
        remembered = record.mapped_job_list_id or (
            state.job_list_id if state is not None and state.job_list_id else ""
        )
        previous = gateway.find_customer_by_id(remembered) if remembered else None
        if previous is not None:
            renamed = gateway.rename_job(record, previous)
            steps.append(f"Job renamed from '{previous.full_name}' to '{renamed.full_name}'.")
            return renamed.list_id, None

        created = gateway.create_job(record, customer)
        steps.append(f"Job '{created.full_name}' created.")
        return created.list_id, None

    def _already_done(
        self, record: WorkRecord, operation: Operation = Operation.CREATE_INVOICE
    ) -> ProcessingResult | None:
        """Has this exact row already been finished in this mode before?"""
        if operation in {Operation.UPDATE_CUSTOMER, Operation.CREATE_BILL}:
            # Updates are always worth re-sending. Bills re-query QuickBooks so
            # a changed Prem inv Memo can be applied to an existing bill.
            return None
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
        customer_name: str = "",
        job_name: str = "",
        invoice_txn_id: str | None = None,
        invoice_ref_number: str | None = None,
        error: str | None = None,
        operation: Operation = Operation.CREATE_INVOICE,
    ) -> ProcessingResult:
        if operation is Operation.CREATE_BILL:
            self.store.save_invoice(
                record,
                self.mode,
                "bill",
                status=status,
                message=message,
                txn_id=invoice_txn_id,
                ref_number=invoice_ref_number,
                error_message=error,
                steps=steps,
            )
        elif operation.invoice_kind:
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
            customer_name=customer_name,
            job_name=job_name,
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
