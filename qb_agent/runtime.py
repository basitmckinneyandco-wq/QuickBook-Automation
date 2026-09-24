"""Glue: holds the loaded spreadsheet, the history, and the processor together."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from threading import Lock, Thread
from time import monotonic, sleep
from typing import Mapping

from .config import AppConfig, database_for, load_config, save_company, save_data_source
from .edits import EditError, apply, apply_all
from .gsheets import Sheets, SheetsError, column_letter, status_columns
from .models import INVOICE_KINDS, Operation, WorkRecord
from .processor import Processor
from .quickbooks import (
    DesktopQuickBooksGateway,
    DryRunQuickBooksGateway,
    RequestProcessorTransport,
    company_files,
    sdk_registered,
    ui_blockers,
)
from .storage import Store
from .websource import WebSourceError, build_records, fetch_tabs, tab_label

# The most policies one request may carry. QuickBooks is worked through one at
# a time behind a single session, so a smaller batch means quicker feedback,
# and a failure costs at most this many rows rather than the whole selection.
MAX_BATCH = 50

# How long a downloaded copy of the Google Sheet stays usable before we re-fetch.
CACHE_SECONDS = 300

# How often the background watcher re-reads the sheet, so rows added in Google
# Sheets turn up on the dashboard without anyone pressing Refresh.
WATCH_SECONDS = 60

# How long the policy -> sheet row index stays usable. Only built when the rows
# arrive through the published link, which does not carry real row numbers.
LOOKUP_SECONDS = 300

# What "Pending" in the sheet means: not a status to display, but an
# instruction to forget what this machine thinks it did and judge the row again.
_RESET = "__reset__"


class Runtime:
    def __init__(self, config: AppConfig):
        self.config = config
        self.store = Store(config.database_path)
        self.records: dict[str, WorkRecord] = {}
        self.source_name = ""
        self.imported_at = ""
        self.import_warnings: list[str] = []
        self.tabs: list[str] = []
        self.active_tab = ""
        self.loading = False
        self.load_error = ""
        # Why the last attempt to write a status into the sheet failed, if it did.
        self.sheet_write_error = ""
        self._cache: dict[str, list[list[object]]] | None = None
        self._cached_at = 0.0
        # How long the feed last took to answer, so the watcher can pace itself
        # against a slow sheet instead of asking again before it has finished.
        self._fetch_seconds = 0.0
        # Bumped whenever the loaded rows change, so the page can tell a real
        # change from a poll that found nothing new and re-render only then.
        self.revision = 0
        self._row_signature = ""
        self._watching = False
        # What the current batch is doing, so the dashboard can tick rows over
        # one at a time instead of waiting for the whole batch to come back.
        self._progress: dict[str, object] = {"active": False}
        # Company name -> file path, for companies kept outside the folders a
        # scan can find. Asking QuickBooks costs a session, so it is asked once.
        self._company_paths: dict[str, str] = {}
        # One dry-run gateway for the whole session, so a customer "created" in
        # an early batch is still found by a later one - just like the real thing.
        self._dry_gateway = DryRunQuickBooksGateway(config.company.display_name)
        self.processor = Processor(config, self.store, self._gateway)
        self._lock = Lock()
        # Separate from the app lock: it serialises downloads only.
        self._fetch_lock = Lock()
        # Signed in once and kept, rather than per read.
        self._sheets_client: Sheets | None = None
        # tab label -> {field: column index} for the four status columns, found
        # from each tab's header when it is read.
        self._status_columns: dict[str, dict[str, int]] = {}
        # tab label -> {policy key: real sheet row}, built only when the rows
        # come from the published link and so carry no true row number.
        self._row_index: dict[str, dict[tuple[str, str, str], int]] = {}
        self._lookup_at = 0.0

    def _gateway(self):
        if self.config.mode == "quickbooks":
            return DesktopQuickBooksGateway(
                company_file=self.config.company.file_path,
                app_name=self.config.app_name,
                qbxml_version=self.config.qbxml_version,
                invoice_template=self.config.invoice_template,
                premium=self.config.premium,
                commission=self.config.commission,
                fees=self.config.fees,
            )
        return self._dry_gateway

    def _read_gateway(self) -> DesktopQuickBooksGateway:
        """A read-only view of QuickBooks, available even in dry-run mode.

        Browsing customers and invoices writes nothing, so it does not need the
        write switches - but it does still verify the company file when one is
        configured.
        """
        return DesktopQuickBooksGateway(
            company_file=self.config.company.file_path,
            app_name=self.config.app_name,
            qbxml_version=self.config.qbxml_version,
        )

    # --- what the dashboard asks for ----------------------------------------

    def health(self) -> dict[str, object]:
        company_path = self.config.company.file_path
        return {
            "mode": self.config.mode,
            "writes_enabled": self.config.live_writes_enabled,
            "blocked_reason": self.config.blocked_reason(),
            "app_name": self.config.app_name,
            "qbxml_version": self.config.qbxml_version,
            "invoice_template": self.config.invoice_template or "(QuickBooks default)",
            "company": {
                "display_name": self.config.company.display_name,
                "file_path": company_path,
                "file_exists": bool(company_path) and Path(company_path).is_file(),
            },
            "sdk_registered": sdk_registered(),
            # What QuickBooks has on screen right now: an open Customer Centre
            # or a modal dialog will spoil a send, and neither is visible to
            # the SDK.
            "quickbooks_ui": ui_blockers(),
            "source_name": self.source_name,
            "imported_at": self.imported_at,
            "import_warnings": self.import_warnings,
            "loaded_rows": len(self.records),
            # Changes only when the rows themselves change, so the page knows
            # when to pull and re-render, and when to leave itself alone.
            "revision": self.revision,
            "watching": self._watching,
            "data_source_enabled": self.config.data_source.enabled,
            "data_source_url": self.config.data_source.url,
            "loading": self.loading,
            "load_error": self.load_error,
            "tabs": self.tabs or ([self.config.data_source.default_tab] if self.config.data_source.default_tab else []),
            "active_tab": self.active_tab,
        }

    def records_payload(self) -> list[dict[str, object]]:
        states = self.store.states_for(list(self.records), self.config.mode)
        invoices = self.store.invoice_states(list(self.records), self.config.mode)
        edits = self.store.overrides()  # read once, not per row
        payload: list[dict[str, object]] = []
        for record in self.records.values():
            item = record.to_dict()
            state = states.get(record.source_id)
            if _is_obsolete_name_limit_review(state):
                state = None
            item.update(
                self._state_payload(record, state, invoices.get(record.source_id, {}))
            )
            item["edited"] = sorted(edits.get(record.source_id, {}))
            payload.append(item)
        return payload

    def _state_payload(
        self, record: WorkRecord, state, invoices: dict[str, object] | None = None
    ) -> dict[str, object]:
        """The customer status and each invoice's status, side by side.

        The sheet has the last word. It is what every dashboard can see, so a
        status written there - by this app or by a person - is what everybody
        shows. Setting a cell back to Pending is therefore a way of saying
        "do this one again", and it is obeyed.

        Where the sheet says nothing, this machine's own history answers, and
        where that says nothing either the row is judged on its own merits.

        Nothing is duplicated by obeying the sheet: QuickBooks is still asked
        whether the customer, job and invoice exist before anything is created,
        so a row wrongly set back to Pending is re-sent and comes back
        "already there" rather than creating a second copy.
        """
        item: dict[str, object] = {}
        elsewhere = self._sheet_says(record)

        # The QuickBooks ids are facts this machine learned by doing the work,
        # so they are kept whatever the sheet says about the status.
        if state is not None:
            item["customer_list_id"] = state.customer_list_id
            item["job_list_id"] = state.job_list_id
            item["invoice_txn_id"] = state.invoice_txn_id
            item["error_message"] = state.error_message

        said = elsewhere.get("qb_customer_job")
        if said == _RESET:
            # "Pending" is somebody saying this one still needs doing, and it
            # wins outright - including over this machine's own record of
            # having done it. That is the point of the column: a second person,
            # or a second company, can put a row back in the queue.
            #
            # It is only safe to obey because the sheet is read directly. Read
            # through the published link, which lags a minute behind, a send
            # would be followed by a read that still said Pending and would
            # undo itself.
            state = None
            said = ""
        if said:
            item["status"] = said
            if _done_elsewhere(said, state is not None and bool(state.status)):
                item["status_elsewhere"] = True
        elif state is not None and state.status:
            item["status"] = state.status
        else:
            item["status"] = "review" if record.review_reasons() else "ready"

        held = invoices or {}
        for kind in INVOICE_KINDS.values():
            saved = held.get(kind)
            from_sheet = elsewhere.get(f"qb_{kind}")
            if from_sheet == _RESET:
                saved = None        # same as above: Pending puts it back in the queue
                from_sheet = ""
            if saved is not None:
                item[f"{kind}_txn_id"] = saved.txn_id
                item[f"{kind}_ref_number"] = saved.ref_number
                item[f"{kind}_error"] = saved.error_message
            if from_sheet:
                item[f"{kind}_status"] = from_sheet
                if _done_elsewhere(from_sheet, saved is not None and bool(saved.status)):
                    item[f"{kind}_elsewhere"] = True
            elif saved is not None and saved.status:
                item[f"{kind}_status"] = saved.status
            else:
                item[f"{kind}_status"] = (
                    "review" if record.invoice_reasons(kind) else "ready"
                )
        return item

    # What each word in the sheet means here.
    #
    # Only the first four record something a machine did, so only those are
    # worth carrying between dashboards. Pending means the opposite - "this one
    # is not done" - and is honoured as a reset: whatever this machine thinks
    # it did, the row is judged afresh.
    #
    # "Needs review" is deliberately absent. It is not a decision anybody made;
    # it is what a dashboard works out when something on the row is missing.
    # Taking it from the sheet would freeze it there - a row whose gaps had
    # since been filled would still read Needs review, for ever.
    _SHEET_STATUS = {
        "created": "completed",
        "already there": "completed",
        "failed": "failed",
        "not needed": "not_needed",
        "pending": _RESET,
    }

    def _sheet_says(self, record: WorkRecord) -> dict[str, str]:
        """What the sheet's four columns claim about this row, if anything."""
        extra = dict(record.extra)
        found: dict[str, str] = {}
        for field in ("qb_customer_job", "qb_premium", "qb_commission", "qb_fees"):
            # Values arrive from a dropdown and are sometimes pasted, so a
            # stray tab or trailing space is normal and must not stop a match.
            word = str(extra.get(field, "")).strip().casefold()
            status = self._SHEET_STATUS.get(word)
            if status:
                found[field] = status
        return found

    def activity(self, limit: int = 200) -> list[dict[str, object]]:
        return [
            {
                "source_id": event.source_id,
                "label": event.label,
                "mode": event.mode,
                "status": event.status,
                "message": event.message,
                "created_at": event.created_at,
                # The technical half of the log: what ran, how far it got and
                # what went wrong, so a failure can be diagnosed after the fact.
                "operation": event.operation,
                "steps": list(event.steps),
                "error": event.error,
                "row_number": event.row_number,
            }
            for event in self.store.recent_events(limit)
        ]

    # --- actions -------------------------------------------------------------

    def _note_revision(self, records: dict[str, WorkRecord]) -> None:
        """Bump the revision only when the rows themselves actually changed.

        The watcher runs every minute whether or not anyone edited the sheet,
        and a revision that moved every minute would have the dashboard
        re-rendering - and re-animating - constantly for no reason.
        """
        material = "\n".join(
            "|".join(
                (
                    source_id,
                    record.customer_name,
                    record.job_name,
                    record.invoice_number,
                    record.invoice_date.isoformat() if record.invoice_date else "",
                    str(record.amount),
                    repr(record.extra),
                )
            )
            for source_id, record in sorted(records.items())
        )
        signature = sha256(material.encode("utf-8")).hexdigest()
        if signature != self._row_signature:
            self._row_signature = signature
            self.revision += 1

    def start_watching(self) -> None:
        """Re-read the sheet every minute so new rows appear on their own."""
        if self._watching or not self.config.data_source.enabled:
            return
        self._watching = True
        Thread(target=self._watch, daemon=True).start()

    def _watch(self) -> None:
        while True:
            # A minute between reads, unless the sheet itself is slower than
            # that: asking again while the last answer is still arriving just
            # queues downloads behind each other for no fresher data.
            sleep(max(WATCH_SECONDS, self._fetch_seconds * 1.5))
            if self.loading:
                continue
            try:
                before = self.revision
                self.fetch_web(force=True)
                if self.revision != before:
                    print(
                        f"Sheet changed - now {len(self.records)} policies.", flush=True
                    )
                # Having just read the sheet, put back anything it does not yet
                # know. Only differences are written, so a settled sheet costs
                # nothing at all.
                done = self.sync_sheet()
                if done.get("written"):
                    print(f"Sheet updated: {done['written']} cells.", flush=True)
            except Exception as exc:
                # A blip in the feed must not kill the watcher; it tries again.
                self.load_error = str(exc)

    def start_prefetch(self, tab: str = "", force: bool = False) -> None:
        """Pull the sheet in the background so the page can render straight away."""
        if not self.config.data_source.enabled or self.loading:
            return
        self.loading = True
        self.load_error = ""
        Thread(target=self._prefetch, args=(tab, force), daemon=True).start()

    def _prefetch(self, tab: str, force: bool) -> None:
        try:
            self.fetch_web(tab, force)
        except Exception as exc:
            self.load_error = str(exc)
            print(f"Could not load the Google Sheet: {exc}", flush=True)
        finally:
            self.loading = False

    def fetch_web(self, tab: str = "", force: bool = False) -> dict[str, object]:
        """Pull the feed and load EVERY tab into one combined list of rows."""
        if not (self.config.google_sheet.enabled or self.config.data_source.enabled):
            raise WebSourceError("No Google Sheet is configured in config.json.")
        # Deliberately outside the lock. Google takes the best part of a minute
        # to answer, and the watcher asks every minute, so holding the lock
        # across the download left almost no gap for an edit or a send to get
        # in - they queued behind the network. Only the swap needs the lock.
        tabs = self._tabs(force)
        with self._lock:
            saved_edits = self.store.overrides()
            records: dict[str, WorkRecord] = {}
            warnings: list[str] = []
            loaded: list[str] = []
            totals = skipped = 0
            columns: dict[str, str] = {}

            for name, rows in tabs.items():
                try:
                    result = build_records(name, rows)
                except WebSourceError as exc:
                    # One unusable tab must not sink the others.
                    warnings.append(f"{tab_label(name)}: {exc}")
                    continue
                for record in result.records:
                    records[record.source_id] = record
                    edits = saved_edits.get(record.source_id)
                    if edits:
                        records[record.source_id] = apply_all(record, edits)
                loaded.append(tab_label(name))
                totals += result.total_rows
                skipped += result.skipped_rows
                warnings.extend(f"{tab_label(name)}: {text}" for text in result.warnings)
                columns.update(result.columns)

            if not records:
                raise WebSourceError(
                    "No tab in the feed produced any rows. " + " ".join(warnings)
                )

            self.records = records
            self._note_revision(records)
            self.tabs = loaded
            self.active_tab = "all"
            self.source_name = "Google Sheet"
            self.imported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.import_warnings = warnings
            # A read that worked clears the last one that did not. Without this
            # a single refused request left the warning on screen for good,
            # while the minute-by-minute reads went on succeeding behind it.
            self.load_error = ""
            return {
                "source_name": self.source_name,
                "tab": "all",
                "tabs": loaded,
                "loaded_rows": len(records),
                "skipped_rows": skipped,
                "total_rows": totals,
                "mapped_columns": columns,
                "warnings": warnings,
                "cached": not force,
            }

    def _tabs(self, force: bool) -> dict[str, list[list[object]]]:
        """The sheet's rows, downloading them only when they are stale.

        Guarded by its own lock, not the app's: two downloads must not overlap,
        but a download must not stop anything else happening meanwhile.
        """
        with self._fetch_lock:
            fresh = self._cache is not None and (monotonic() - self._cached_at) < CACHE_SECONDS
            if force or not fresh:
                started = monotonic()
                settings = self.config.google_sheet
                self._cache = (
                    self._read_sheet()
                    if settings.enabled and settings.read_rows
                    else fetch_tabs(self.config.data_source.url)
                )
                self._cached_at = monotonic()
                self._fetch_seconds = self._cached_at - started
            assert self._cache is not None
            return self._cache

    def _read_sheet(self) -> dict[str, list[list[object]]]:
        """Read the tabs straight from the spreadsheet, as the service account.

        Each tab comes back whole and unfiltered, so a row's position in the
        list is its real position in the sheet - which is what makes writing a
        status back to the right row possible at all.
        """
        settings = self.config.google_sheet
        sheets = self._sheets()
        wanted = settings.tab_titles
        if not wanted:
            raise WebSourceError(
                "No tabs are listed under google_sheet in config.json, so there "
                "is nothing to read."
            )
        grids: dict[str, list[list[object]]] = {}
        missing: list[str] = []
        available = None
        for label, title in wanted.items():
            try:
                grid = sheets.read(title)
                grids[label] = grid
                # Where this tab keeps its four status columns. Found per tab,
                # because the two pipelines are a column out from each other.
                self._status_columns[label] = status_columns(grid)
            except SheetsError as exc:
                if available is None:
                    available = sheets.tab_titles()
                if title not in available:
                    missing.append(title)
                    continue
                raise WebSourceError(str(exc)) from exc
        if missing:
            raise WebSourceError(
                "These tabs are not in the spreadsheet: " + ", ".join(missing)
            )
        return grids

    def _sheets(self) -> Sheets:
        """One client per spreadsheet, so the sign-in is not repeated each minute."""
        settings = self.config.google_sheet
        if self._sheets_client is None or self._sheets_client.spreadsheet_id != settings.spreadsheet_id:
            self._sheets_client = Sheets(settings.key_file, settings.spreadsheet_id)
        return self._sheets_client

    def edit(self, source_id: str, values: Mapping[str, str]) -> dict[str, object]:
        """Correct one or more fields of a row, and remember the corrections.

        Every value is validated before anything is written, so a single bad
        entry cannot leave the row half-updated.
        """
        with self._lock:
            record = self.records.get(source_id)
            if record is None:
                raise EditError("That policy is no longer loaded. Refresh and try again.")

            updated = record
            for field, value in values.items():
                updated = apply(updated, field, str(value))  # raises EditError

            self.records[source_id] = updated
            for field, value in values.items():
                self.store.set_override(source_id, field, str(value))
            return self._record_payload(updated)

    def set_invoice_needed(self, source_id: str, kind: str, needed: bool) -> dict[str, object]:
        """Mark one kind of invoice as not needed for one policy, or wanted again."""
        if kind not in set(INVOICE_KINDS.values()):
            raise ValueError(f"There is no {kind!r} invoice.")
        with self._lock:
            record = self.records.get(source_id)
            if record is None:
                raise ValueError("That policy is no longer loaded. Refresh and try again.")
            self.store.set_invoice_needed(source_id, self.config.mode, kind, needed)

        # Told to the sheet before the answer is built, not after. The row in
        # memory still holds whatever the sheet said when it was last read, and
        # an answer worked out from that stale word reports the state before
        # the click - so the button appeared to do nothing until the next
        # minute's read caught up.
        # Outside the lock, and never fatal: the mark is saved either way.
        self._publish_status([{
            "source_id": source_id,
            "operation": f"{kind}_invoice",
            "status": "ready" if needed else "not_needed",
        }])
        with self._lock:
            record = self.records.get(source_id)
            return self._record_payload(record) if record is not None else {}

    def edit_many(
        self, source_ids: list[str], values: Mapping[str, str]
    ) -> dict[str, object]:
        """Apply the same corrections to many rows at once.

        Each row is validated on its own, so one that cannot take a value is
        reported and skipped rather than sinking the rest of the batch.
        """
        if not source_ids:
            raise EditError("No policies were selected.")
        if not values:
            raise EditError("No fields were given to change.")

        with self._lock:
            updated: list[dict[str, object]] = []
            failed: list[dict[str, str]] = []
            for source_id in source_ids:
                record = self.records.get(source_id)
                if record is None:
                    failed.append({"source_id": source_id, "error": "No longer loaded."})
                    continue
                try:
                    changed = record
                    for field, value in values.items():
                        changed = apply(changed, field, str(value))
                except EditError as exc:
                    failed.append({"source_id": source_id, "error": str(exc)})
                    continue
                self.records[source_id] = changed
                for field, value in values.items():
                    self.store.set_override(source_id, field, str(value))
                updated.append(self._record_payload(changed))
            return {"updated": updated, "failed": failed}

    def _record_payload(self, record: WorkRecord) -> dict[str, object]:
        item = record.to_dict()
        state = self.store.get(record.source_id, self.config.mode)
        if _is_obsolete_name_limit_review(state):
            state = None
        invoices = self.store.invoice_states([record.source_id], self.config.mode)
        item.update(
            self._state_payload(record, state, invoices.get(record.source_id, {}))
        )
        item["edited"] = sorted(self.store.overrides().get(record.source_id, {}))
        return item

    def preflight(self) -> dict[str, object]:
        """Check every name the invoices reference really exists in QuickBooks.

        These names are plain text matched character-for-character. Renaming a
        template or moving an account in QuickBooks breaks every invoice of
        that kind, and without this the first sign would be a failed batch.
        """
        checks: list[dict[str, object]] = []
        try:
            with self._read_gateway() as gateway:
                company = gateway.current_company_file()
                found = gateway.names_in_quickbooks()
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "checks": [],
                "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }

        def check(label: str, value: str, pool: set[str], where: str) -> None:
            if not value:
                return
            checks.append({
                "label": label,
                "value": value,
                "ok": value in pool,
                "where": where,
            })

        for kind, settings in (
            ("Premium", self.config.premium),
            ("Commission", self.config.commission),
            ("Fees", self.config.fees),
        ):
            check(f"{kind} A/R account", settings.ar_account, found["accounts"], "Chart of Accounts")
            check(f"{kind} template", settings.template, found["templates"], "Templates")
            check(f"{kind} item", settings.item, found["items"], "Item List")
            check(
                f"{kind} income account",
                settings.item_income_account,
                found["accounts"],
                "Chart of Accounts",
            )

        # The premium invoice picks its item from Carrier Type, so both must exist.
        for name in ("Standard", "Non-Standard"):
            check(f"Premium item '{name}'", name, found["items"], "Item List")

        # These the app adds on demand, so they are reported, never failed.
        notes = [
            {"label": "Classes", "value": _join(found["classes"]), "where": "Class List"},
            {"label": "Transaction Types", "value": _join(found["terms"]), "where": "Terms List"},
            {"label": "Sources", "value": _join(found["reps"]), "where": "Rep List"},
            {"label": "Carrier items", "value": f"{len(found['items'])} in the Item List",
             "where": "Item List"},
        ]
        return {
            "ok": all(row["ok"] for row in checks),
            "error": "",
            "company": company,
            "checks": checks,
            "notes": notes,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def process(
        self, source_ids: list[str], operation: Operation = Operation.CREATE_INVOICE
    ) -> list[dict[str, object]]:
        # Wait out a sheet refresh, but never queue behind another batch: if
        # QuickBooks has stopped answering - a crash dialog waiting for someone
        # to click it will do this - that batch is stuck inside a call which
        # cannot be cancelled, and waiting would wedge the dashboard too.
        self._take_lock(45)
        try:
            if len(source_ids) > MAX_BATCH:
                raise ValueError(f"A batch cannot contain more than {MAX_BATCH} policies.")
            selected: list[WorkRecord] = []
            unknown: list[dict[str, object]] = []
            for source_id in source_ids:
                record = self.records.get(source_id)
                if record is None:
                    unknown.append(
                        {
                            "source_id": source_id,
                            "label": "Unknown policy",
                            "status": "failed",
                            "message": "That policy is no longer loaded. Refresh and try again.",
                            "dry_run": not self.config.live_writes_enabled,
                            "steps": [],
                        }
                    )
                    continue
                selected.append(record)
            if not selected:
                return unknown

            queue = [record.source_id for record in selected]
            finished: list[dict[str, object]] = []
            self._progress = {
                "active": True,
                "operation": operation.value,
                "total": len(selected),
                "done": 0,
                "queue": list(queue),
                "current": queue[0],
                "finished": finished,
            }

            def report(result) -> None:
                item = result.to_dict()
                finished.append(
                    {
                        "source_id": item["source_id"],
                        "status": item["status"],
                        "message": item["message"],
                        "label": item["label"],
                        "operation": item.get("operation", operation.value),
                    }
                )
                if result.source_id in queue:
                    queue.remove(result.source_id)
                self._progress["done"] = len(finished)
                self._progress["queue"] = list(queue)
                self._progress["current"] = queue[0] if queue else None

            try:
                results = [
                    result.to_dict()
                    for result in self.processor.process_many(selected, operation, report)
                ]
            finally:
                self._progress = {"active": False}
        finally:
            self._lock.release()

        # Outside the lock: writing to the sheet is network work, and must not
        # hold up an edit or the next send. A sheet that cannot be written is
        # reported but never fails the batch - the work in QuickBooks is done
        # either way, and the sheet is only how the other dashboard hears of it.
        self._publish_status(results)
        return results + unknown

    # ── telling the sheet what happened ─────────────────────────────────

    # Which column each operation reports into.
    _STATUS_FIELD = {
        "create_customer": "qb_customer_job",
        "update_customer": "qb_customer_job",
        "create_invoice": "qb_customer_job",
        "premium_invoice": "qb_premium",
        "commission_invoice": "qb_commission",
        "fees_invoice": "qb_fees",
    }
    # What the sheet says. "Already there" is written as Created on purpose:
    # a reader of the sheet only needs to know the work is done, not who did it.
    _STATUS_WORD = {
        "completed": "Created",
        "already_exists": "Created",
        "needs_review": "Needs review",
        "failed": "Failed",
        "not_needed": "Not needed",
        "ready": "Pending",
    }

    # The word this app writes for each status it can show.
    _SHEET_WORD = {
        "completed": "Created",
        "already_exists": "Created",
        "needs_review": "Needs review",
        "review": "Needs review",
        "failed": "Failed",
        "not_needed": "Not needed",
        "ready": "Pending",
    }
    _SHEET_FIELDS = (
        ("status", "qb_customer_job"),
        ("premium_status", "qb_premium"),
        ("commission_status", "qb_commission"),
        ("fees_status", "qb_fees"),
    )

    def sync_sheet(self, limit: int = 4000) -> dict[str, object]:
        """Make the sheet say what the dashboard says, for every loaded row.

        Only cells that differ are written. Everything the dashboard shows is
        already derived from the sheet where the sheet has an opinion, so a
        second run has nothing left to do - there is no tug of war between the
        two, and no write when nothing has changed.
        """
        settings = self.config.google_sheet
        if not (settings.enabled and settings.write_status):
            return {"written": 0, "reason": "Writing to the sheet is switched off."}

        with self._lock:
            states = self.store.states_for(list(self.records), self.config.mode)
            invoices = self.store.invoice_states(list(self.records), self.config.mode)
            rows = list(self.records.values())

        updates: list[tuple[str, str]] = []
        unplaced = 0
        for record in rows:
            state = states.get(record.source_id)
            if _is_obsolete_name_limit_review(state):
                state = None
            shown = self._state_payload(record, state, invoices.get(record.source_id, {}))
            held = dict(record.extra)
            for field, column in self._SHEET_FIELDS:
                word = self._SHEET_WORD.get(str(shown.get(field, "")), "")
                if not word or str(held.get(column, "")).strip() == word:
                    continue                      # nothing to say, or already says it
                cell = self._cell_for(record, column)
                if not cell:
                    unplaced += 1
                    continue
                updates.append((cell, word))
                self._remember(record.source_id, column, word)
            if len(updates) >= limit:
                break

        written = 0
        try:
            # Google counts requests, not cells, so this goes in as few as possible.
            for start in range(0, len(updates), 500):
                written += self._sheets().write(updates[start:start + 500])
        except SheetsError as exc:
            self.sheet_write_error = str(exc)
            return {"written": written, "error": str(exc)}
        self.sheet_write_error = ""
        return {"written": written, "rows": len(rows), "unplaced": unplaced}

    def _remember(self, source_id: str, column: str, word: str) -> None:
        """Note what the sheet now says about one row, having just written it."""
        record = self.records.get(source_id)
        if record is None:
            return
        extra = dict(record.extra)
        if str(extra.get(column, "")).strip() == word:
            return
        extra[column] = word
        self.records[source_id] = replace(record, extra=tuple(extra.items()))

    def _cell_for(self, record: WorkRecord, column: str) -> str:
        """The A1 address of one status column on one policy's row."""
        row = self._sheet_row(record)
        index = self._status_columns.get(record.source_tab, {}).get(column)
        title = self.config.google_sheet.tab_titles.get(record.source_tab, "")
        if row < 1 or index is None or not title:
            return ""
        return f"'{title}'!{column_letter(index)}{row}"

    def _publish_status(self, results: list[dict[str, object]]) -> None:
        """Write each result into its row's status column in the sheet."""
        settings = self.config.google_sheet
        if not (settings.enabled and settings.write_status and results):
            return
        updates: list[tuple[str, str]] = []
        for item in results:
            source_id = str(item.get("source_id", ""))
            operation = str(item.get("operation", ""))
            cell = self._status_cell(source_id, operation)
            word = self._STATUS_WORD.get(str(item.get("status", "")))
            if cell and word:
                updates.append((cell, word))
                # The row in memory still holds what the sheet said when it was
                # last read. Left stale, the next sync would read that old word
                # back and write it over the one being sent now - undoing the
                # result of the very send that produced it.
                column = self._STATUS_FIELD.get(operation, "")
                if column:
                    self._remember(source_id, column, word)
        if not updates:
            return
        try:
            self._sheets().write(updates)
        except SheetsError as exc:
            self.sheet_write_error = str(exc)
        else:
            self.sheet_write_error = ""

    def _status_cell(self, source_id: str, operation: str) -> str:
        """The A1 address of one row's status column, or "" if it cannot be found."""
        record = self.records.get(source_id)
        field = self._STATUS_FIELD.get(operation)
        if record is None or not field:
            return ""
        return self._cell_for(record, field)

    def _sheet_row(self, record: WorkRecord) -> int:
        """Which row of the real spreadsheet this policy is.

        Read straight from the sheet, the row number is already true. Read
        through the published link it is not: that export is filtered before it
        arrives, so its fifth row may be the sheet's hundredth. The policy is
        then found by what identifies it - corporation, policy number and
        effective date - and a key that matches more than one row is refused
        rather than guessed at, because writing to the wrong row is worse than
        not writing at all.
        """
        if self.config.google_sheet.read_rows:
            return record.row_number
        return self._row_lookup(record.source_tab).get(_policy_key(record), 0)

    def _row_lookup(self, label: str) -> dict[tuple[str, str, str], int]:
        """Policy -> real sheet row, for one tab. Built from the sheet, and cached."""
        fresh = (monotonic() - self._lookup_at) < LOOKUP_SECONDS
        if fresh and label in self._row_index:
            return self._row_index[label]
        title = self.config.google_sheet.tab_titles.get(label, "")
        if not title:
            return {}
        try:
            grid = self._sheets().read(title)
        except SheetsError as exc:
            self.sheet_write_error = str(exc)
            return {}
        self._status_columns[label] = status_columns(grid)
        try:
            found = build_records(label, grid)
        except WebSourceError:
            return {}
        seen: dict[tuple[str, str, str], int] = {}
        ambiguous: set[tuple[str, str, str]] = set()
        for entry in found.records:
            key = _policy_key(entry)
            if key in seen:
                ambiguous.add(key)
            seen[key] = entry.row_number
        for key in ambiguous:
            seen.pop(key, None)      # two rows look the same; write to neither
        self._row_index[label] = seen
        self._lookup_at = monotonic()
        return seen

    def _take_lock(self, seconds: float) -> None:
        """Wait a while for the app to be free, then explain who is holding it.

        The sheet watcher re-reads the feed every minute and holds the lock for
        as long as that download takes, so a click can easily land mid-refresh.
        That is worth waiting out - only a real batch is worth refusing for.
        """
        if self._lock.acquire(timeout=seconds):
            return
        if self._progress.get("active"):
            done = self._progress.get("done", 0)
            total = self._progress.get("total", 0)
            raise ValueError(
                f"A batch is still running ({done} of {total}). Wait for it to "
                "finish, then try again. If QuickBooks is showing a message box, "
                "click it away - the app is waiting for QuickBooks to answer."
            )
        raise ValueError(
            "The app is busy reading the Google Sheet. Give it a few seconds "
            "and try again."
        )

    def use_data_source(self, url: str) -> dict[str, object]:
        """Point the app at a different published sheet.

        The link is tried before it is kept. A wrong one - the spreadsheet's
        own page rather than its published data, or a link cut short in an
        email - fails here with a reason, instead of leaving the dashboard
        silently empty and the settings file needing a hand edit to undo.
        """
        if not self.config.source_path:
            raise ValueError("This app was started without a settings file to write back to.")
        url = url.strip()
        if not url:
            raise ValueError("Paste the link first.")
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("That is not a link. It should start with https://")
        if "docs.google.com/spreadsheets" in url.lower():
            raise ValueError(
                "That is the link to the spreadsheet page, which the app cannot read. "
                "Use the published link from the Apps Script web app - it starts with "
                "https://script.googleusercontent.com/ or https://script.google.com/."
            )

        self._take_lock(45)
        try:
            tabs = fetch_tabs(url)          # the real test: can it be read at all?
            rows = sum(len(grid) for grid in tabs.values())
            if not rows:
                raise ValueError("That link works, but there is no data behind it.")
            save_data_source(self.config, url)
            self.config = load_config(self.config.source_path)
            self._cache = tabs              # already fetched, so do not fetch twice
            self._cached_at = monotonic()
        finally:
            self._lock.release()

        # Outside the lock: loading the rows takes it again.
        self.fetch_web(force=False)
        return {"url": url, "tabs": sorted(tabs), "rows": len(self.records)}

    def companies(self) -> dict[str, object]:
        """The company files on this machine, and which one is which.

        `open_in_quickbooks` matters more than it looks: QuickBooks serves one
        company at a time, so while it is running that is the only company the
        app can be switched to. The list is still worth showing, because with
        QuickBooks closed any of them can be picked and QuickBooks will start it.
        """
        current = self.config.company.file_path
        # Keyed by path, because that is a company's only reliable identity:
        # two files in different folders can share a name, and the name shown
        # in QuickBooks is the company name, which need not match the file at all.
        found: dict[str, str] = {}
        for item in company_files(current):
            found.setdefault(_key(item["file_path"]), item["file_path"])
        if current:
            found.setdefault(_key(current), current)
        # Companies found once through QuickBooks stay listed after it moves on.
        for path in self._company_paths.values():
            found.setdefault(_key(path), path)

        # What QuickBooks has open. The window title gives the company name for
        # free; the file path costs a session, and only that can be matched.
        open_name = str(ui_blockers().get("open_company", ""))
        open_path = self._open_company_path(open_name) if open_name else ""
        if open_path:
            found.setdefault(_key(open_path), open_path)

        items = [
            {
                "display_name": Path(path).stem,
                "file_path": path,
                "folder": str(Path(path).parent),
                "current": _key(path) == _key(current),
                "open": bool(open_path) and _key(path) == _key(open_path),
            }
            for path in found.values()
        ]
        # While QuickBooks' permission request is waiting, the path cannot be
        # read - so the open company is offered by name, to be adopted as
        # "whatever is open" once that request is answered.
        if open_name and not open_path:
            items.append({
                "display_name": open_name,
                "file_path": "",
                "folder": "",
                "current": False,
                "open": True,
            })
        items.sort(key=lambda item: (item["display_name"].casefold(), item["folder"].casefold()))
        # Only spell out the folder when two companies share a name.
        names = [item["display_name"].casefold() for item in items]
        for item in items:
            item["ambiguous"] = names.count(item["display_name"].casefold()) > 1
        return {
            "companies": items,
            "open_company": open_name,
            "open_file": open_path,
            "quickbooks_running": bool(open_name),
            "current_file": current,
        }

    def _open_company_path(self, name: str) -> str:
        """Where the company QuickBooks has open actually lives, remembered."""
        key = name.casefold()
        if key in self._company_paths:
            return self._company_paths[key]
        try:
            with RequestProcessorTransport("", self.config.app_name) as transport:
                path = transport.current_company_file()
        except Exception:
            return ""                     # QuickBooks busy or access not granted
        path = str(Path(path).resolve()) if path.strip() else ""
        if path:
            self._company_paths[key] = path
        return path

    def _prove_company(self, company_file: str) -> None:
        """Refuse a company the app would not actually be able to talk to.

        QuickBooks serves one company at a time. Picking a second one while it
        is running fails at the next send, not here, so the session is opened
        now and the failure explained in terms of what to do about it.
        """
        want = Path(company_file).stem
        try:
            with RequestProcessorTransport(company_file, self.config.app_name):
                return
        except Exception as exc:
            open_name = str(ui_blockers().get("open_company", ""))
            if open_name and open_name.casefold() != want.casefold():
                raise ValueError(
                    f"QuickBooks has {open_name} open, and it can only serve one company "
                    f"at a time. In QuickBooks choose File, Open Previous Company, {want} "
                    f"- then pick it here."
                ) from exc
            raise ValueError(
                f"QuickBooks would not open {want}. If a permission request is waiting on "
                f"screen, answer it with 'Yes, whenever this company file is open', then "
                f"try again. ({exc})"
            ) from exc

    def use_company(self, company_file: str = "") -> dict[str, object]:
        """Point the app at a different QuickBooks company.

        With no path it adopts whichever company QuickBooks currently has
        open, which is the only one it could talk to anyway. The history
        database is switched with it: that file records what has already been
        created, and carrying it across would have the app skip real work.
        """
        if not self.config.source_path:
            raise ValueError("This app was started without a settings file to write back to.")
        # Never swap the company out from under a batch that is mid-flight,
        # but do wait out an ordinary sheet refresh.
        self._take_lock(45)
        try:
            if not company_file.strip():
                with RequestProcessorTransport("", self.config.app_name) as transport:
                    company_file = transport.current_company_file()
                company_file = str(Path(company_file).resolve())
            else:
                company_file = str(Path(company_file).resolve())
                if not Path(company_file).is_file():
                    raise ValueError(f"There is no company file at {company_file}.")
                self._prove_company(company_file)

            display = Path(company_file).stem
            # Only start a new history when the company actually changes.
            # Re-selecting the company already in use must keep everything
            # this app has recorded about it.
            same = _same_file(company_file, self.config.company.file_path)
            database = (
                self.config.database_path
                if same and self.config.database_path
                else database_for(company_file, self.config.source_path)
            )
            save_company(self.config, company_file, display, database)

            # Reload rather than patch, so what runs matches what is on disk.
            self.config = load_config(self.config.source_path)
            self.store = Store(self.config.database_path)
            self.processor = Processor(self.config, self.store, self._gateway)
            self._dry_gateway = DryRunQuickBooksGateway(self.config.company.display_name)
            return {
                "company": display,
                "file_path": company_file,
                "database": Path(database).name,
            }
        finally:
            self._lock.release()

    def progress(self) -> dict[str, object]:
        """What the batch in flight has finished so far, and what is next."""
        return dict(self._progress)


# Statuses that mean somebody actually did something. "Pending" and "Needs
# review" are what any dashboard works out for itself from the row, so finding
# them in the sheet says nothing about who has touched it.
_ACTED = {"completed", "failed", "not_needed"}


def _done_elsewhere(status: str, known_here: bool) -> bool:
    """Should this badge be marked as another machine's work?

    Only when somebody acted, and this machine has no record of being the one
    who did. The app writes its own results into the sheet and reads them back
    a minute later, so without the second test every row would carry the mark
    and it would tell nobody anything.
    """
    return status in _ACTED and not known_here


def _policy_key(record: WorkRecord) -> tuple[str, str, str]:
    """What identifies one policy row well enough to find it in the sheet."""
    return (
        record.customer_name.strip().casefold(),
        record.invoice_number.strip().casefold(),
        record.invoice_date.isoformat() if record.invoice_date else "",
    )


def _key(path: str) -> str:
    """One spelling per file, so the same company is never listed twice."""
    if not path.strip():
        return ""
    try:
        return str(Path(path).resolve()).casefold()
    except OSError:
        return path.strip().casefold()


def _same_file(left: str, right: str) -> bool:
    """Two paths pointing at one file, whatever the spelling or capitals."""
    if not left or not right:
        return False
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return left.strip().casefold() == right.strip().casefold()


def _join(values: set[str], limit: int = 8) -> str:
    """A readable list of names, trimmed when there are too many to show."""
    names = sorted(value for value in values if value)
    if not names:
        return "none yet"
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f" and {len(names) - limit} more"


def _is_obsolete_name_limit_review(state: object | None) -> bool:
    """Ignore rows blocked only by the pre-shortening customer-name rule."""
    return bool(
        state
        and getattr(state, "status", "") == "needs_review"
        and "Customer name is longer than the 41 characters QuickBooks allows."
        in (getattr(state, "error_message", "") or "")
    )


__all__ = ["Runtime", "MAX_BATCH"]
