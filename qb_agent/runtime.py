"""Glue: holds the loaded spreadsheet, the history, and the processor together."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import re
from threading import Lock, Thread
from time import monotonic, sleep
from typing import Mapping

from .config import AppConfig, database_for, load_config, save_company, save_data_source
from .edits import EditError, apply, apply_all
from .gsheets import (
    MAPPING_HEADERS,
    Sheets,
    SheetsError,
    column_letter,
    dashboard_header_row,
    sheet_columns,
    status_columns,
)
from .models import INVOICE_KINDS, Operation, WorkRecord
from .processor import Processor
from .quickbooks import (
    MAX_SHIP_VIA_NAME,
    DesktopQuickBooksGateway,
    DryRunQuickBooksGateway,
    RequestProcessorTransport,
    company_files,
    same_company_file,
    sdk_registered,
    ui_blockers,
)
from .storage import Store
from .websource import TAB_LABELS, WebSourceError, build_records, fetch_tabs, tab_label

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

# These do not distinguish one insured location from another.  They are kept
# out of the location match so "LOC 1903" and "1903 Hamilton Rd" still agree.
_LOCATION_FILLERS = frozenset({
    "loc", "location", "llc", "inc", "ltd", "co", "company", "corp",
    "corporation", "the", "and", "of",
})


def _location_words(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.casefold()))


# The four columns a dashboard writes, as the rows carry them.
STATUS_FIELDS = ("qb_customer_job", "qb_premium", "qb_commission", "qb_fees")
MAPPING_FIELDS = ("qb_customer_target", "qb_customer_id", "qb_customer_action")
RESOLUTION_FIELDS = (*MAPPING_FIELDS, "old_policy_number", "location")

# Words that record something having happened, and so may be written back into
# the sheet on their own. "Pending" and "Needs review" are deliberately absent:
# they say the app has nothing to report, and sending them every few seconds
# would overwrite whatever a person is in the middle of typing.
_SETTLED = frozenset({"Created", "Failed", "Not needed"})

# How often those columns are re-read straight from the sheet. Two seconds a
# read, so this can run often: someone typing a status into the sheet expects
# to see it on the dashboard about as fast as they would see it on a colleague's
# screen, not a minute later.
STATUS_WATCH_SECONDS = 15


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
        # (policy, status column) -> when this machine last finished writing
        # that cell. A read of the sheet that began before then may have been
        # answered from before the write, so it says nothing about that cell.
        self._written_at: dict[tuple[str, str], float] = {}
        # One status read at a time. The watcher and a Refresh click can land
        # together, and two passes seeing the same fresh "Pending" would both
        # act on it.
        self._status_lock = Lock()

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
                transaction_types=self.config.transaction_types,
                source_aliases=dict(self.config.source_aliases),
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
            # Why the sheet could not be written, if it could not. Silence here
            # is how a machine ends up creating things in QuickBooks while
            # nobody else ever hears about it.
            "sheet_write_error": self.sheet_write_error,
            "sheet_enabled": self.config.google_sheet.enabled,
            # Both kinds of business always, then anything else the feed had.
            # Built from what arrived, the list lost New Business whenever
            # none of its rows happened to be ready, and showed the raw
            # setting "nb" before the first download finished.
            "tabs": list(dict.fromkeys([*TAB_LABELS.values(), *self.tabs])),
            "active_tab": self.active_tab,
        }

    def records_payload(self) -> list[dict[str, object]]:
        states = self.store.states_for(list(self.records), self.config.mode)
        invoices = self.store.invoice_states(list(self.records), self.config.mode)
        edits = self.store.overrides()  # read once, not per row
        payload: list[dict[str, object]] = []
        for record in self.records.values():
            item = record.to_dict()
            item["transaction_type"] = self._shown_transaction_type(record)
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

        # Pending typed into the sheet says "this one still needs doing". It is
        # obeyed as an instruction - whatever was done before is set aside - but
        # it is not a status to display: the badge then says what is true of the
        # row, which is Pending when it is ready to send and Needs review when
        # something on it would stop QuickBooks accepting it. Showing Pending on
        # a row that cannot be sent would be the dashboard lying politely.
        said = elsewhere.get("qb_customer_job")
        if said == _RESET:
            # Pending has already done its work by the time we get here:
            # honour_resets() deletes this machine's record of the row the
            # moment it sees the word. So a record that still exists was made
            # AFTER the reset - the work was done since - and it is the newer
            # fact of the two.
            #
            # Discarding it here as well was what trapped a row in Pending: the
            # send succeeded, the sheet still said Pending for a moment, the
            # dashboard therefore showed Pending, and the sync wrote that back.
            # Round it went, with a customer sitting in QuickBooks all the while.
            said = ""           # judged on the record below, or on the row
        # Same for the customer: a refusal is about the row, and the row can be
        # corrected.
        if state is not None and state.status == "needs_review":
            state = None

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
            # "Needs review" is a judgement about the row, not something that
            # happened in QuickBooks: it means a send was refused before it
            # started. Keeping it would freeze that verdict - fill in the
            # missing commission and the row would still read Needs review,
            # for ever, because of one refusal weeks ago. It is worked out
            # again every time instead. Created, Failed and Not needed all
            # record real events and are kept.
            if saved is not None and saved.status == "needs_review":
                saved = None
            from_sheet = elsewhere.get(f"qb_{kind}")
            if from_sheet == _RESET:
                # Same as above: the reset has already cleared any mark, so a
                # mark that is still here was made since, and stands.
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
            elif not record.invoice_applies(kind):
                # Nothing to bill, so nothing to do and nothing to fix.
                item[f"{kind}_status"] = "not_needed"
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

    def start_status_watch(self) -> None:
        """Keep the four status columns current, whatever the row feed is doing."""
        settings = self.config.google_sheet
        if not settings.enabled or settings.read_rows:
            return          # reading the sheet directly already does this
        Thread(target=self._watch_statuses, daemon=True).start()

    def _watch_statuses(self) -> None:
        """A minute apart, and quick: one read of each tab, two seconds or so.

        Kept separate from the row watcher on purpose. That one goes through
        the published link, which has been measured at well over two minutes
        and fails about half the time - far too slow for someone watching to
        see a colleague's work appear.
        """
        while True:
            sleep(STATUS_WATCH_SECONDS)
            try:
                changed = self.refresh_statuses()
                if changed:
                    print(f"Statuses read from the sheet: {changed} changed.", flush=True)
            except Exception as exc:
                self.sheet_write_error = str(exc)

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
                # The rows have just arrived through the link, and its copy of
                # the status columns is minutes old. Read those again from the
                # sheet before deciding what to write back, or a status someone
                # typed in the meantime is compared against the stale copy and
                # promptly overwritten with what the app thought it was.
                self.refresh_statuses()

                # Put the sheet back in step with the dashboard. Only cells
                # that differ are written, so a settled sheet costs nothing.
                done = self.sync_sheet()
                if done.get("written"):
                    print(f"Sheet updated: {done['written']} cells.", flush=True)
                if done.get("unplaced"):
                    self.sheet_write_error = (
                        f"{done['unplaced']} status(es) could not be written: their row "
                        "in the sheet could not be identified - either no row matches "
                        "the corporation, policy number and effective date, or more "
                        "than one does and writing to either would be a guess."
                    )
                    print(f"Sheet: {self.sheet_write_error}", flush=True)
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
                self._status_columns[label] = sheet_columns(grid)
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

    def _shown_transaction_type(self, record: WorkRecord) -> str:
        """The Type column: what will actually go on the invoice.

        The row's own Policy type column when it has one, and otherwise the
        name the settings give this tab. Showing the bare tab name instead left
        the dashboard saying one thing and the invoice another, with no way to
        tell from the screen which would be used.
        """
        return record.transaction_type or self.config.transaction_types.name_for(
            record.source_tab
        )

    def _record_payload(self, record: WorkRecord) -> dict[str, object]:
        item = record.to_dict()
        item["transaction_type"] = self._shown_transaction_type(record)
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

        # The premium invoice picks its item from Carrier Type, so both must
        # exist - under whatever this company file calls them.
        for name in (self.config.premium.item_standard, self.config.premium.item_non_standard):
            check(f"Premium item '{name}'", name, found["items"], "Item List")

        # Checked, not added on demand. This list is a vocabulary somebody set
        # up - New Business, Endorsement, Rewrite - so a name that is not on it
        # is a typo in the settings, and the invoice would come out blank.
        # Cut to 15 characters as QuickBooks cuts it, so "Renewal Business"
        # in the settings is recognised as the list's "Renewal Busines".
        for name in (self.config.transaction_types.new_biz, self.config.transaction_types.renewal):
            check(
                f"Transaction Type '{name}'",
                name.strip()[:MAX_SHIP_VIA_NAME],
                found["transaction_types"],
                "Ship Via List",
            )

        # These the app adds on demand, so they are reported, never failed.
        notes = [
            {"label": "Classes", "value": _join(found["classes"]), "where": "Class List"},
            {"label": "Transaction Types", "value": _join(found["transaction_types"]),
             "where": "Ship Via List"},
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

    def customer_resolutions(self, source_ids: list[str]) -> dict[str, object]:
        """Classify rows, using a Renewal policy or New Business location first."""
        if not source_ids:
            raise ValueError("Select at least one policy first.")
        if len(source_ids) > MAX_BATCH:
            raise ValueError(f"A batch cannot contain more than {MAX_BATCH} policies.")

        # The published feed intentionally carries only the verified accounting
        # block. Read column R and saved IDs straight from the real sheet first.
        self._refresh_resolution_fields(source_ids)
        records = [self.records[source_id] for source_id in source_ids if source_id in self.records]
        resolved: list[str] = []
        ambiguous: list[dict[str, object]] = []
        automatic: list[dict[str, str]] = []
        factory = self._read_gateway if self.config.mode == "quickbooks" else self._gateway
        candidate_cache: dict[str, list[object]] = {}

        with factory() as gateway:
            for record in records:
                if record.mapped_customer_list_id:
                    selected = gateway.find_customer_by_id(record.mapped_customer_list_id)
                    if selected is not None:
                        resolved.append(record.source_id)
                        continue
                    reason = (
                        "The customer picked for this policy before has since been deleted "
                        "from QuickBooks. Pick again."
                    )
                elif record.create_new_customer_selected:
                    resolved.append(record.source_id)
                    continue
                else:
                    old_matches, old_is_exact = self._old_policy_customers(gateway, record)
                    if len(old_matches) == 1 and old_is_exact:
                        selected = old_matches[0]
                        automatic.append({
                            "source_id": record.source_id,
                            "action": "old_policy",
                            "list_id": selected.list_id,
                        })
                        continue
                    if old_matches:
                        reason = (
                            f"The expiring policy {record.old_policy_number} was billed to more "
                            "than one customer in QuickBooks, so this one cannot be worked out "
                            "on its own. Pick the right one."
                            if len(old_matches) > 1
                            else f"The expiring policy {record.old_policy_number} is in QuickBooks "
                            "but under a shortened number, so it could not be confirmed. Check "
                            "this is the right customer."
                        )
                        candidates = old_matches
                        ambiguous.append({
                            "source_id": record.source_id,
                            "customer_name": record.customer_name,
                            "policy_number": record.invoice_number,
                            "old_policy_number": record.old_policy_number,
                            "job_name": record.job_name,
                            "reason": reason,
                            "candidates": [
                                {"list_id": item.list_id, "full_name": item.full_name}
                                for item in candidates
                            ],
                        })
                        continue
                    location_candidates, location_matches = self._new_business_location_customers(
                        gateway, record, candidate_cache
                    )
                    if len(location_matches) == 1:
                        selected = location_matches[0]
                        automatic.append({
                            "source_id": record.source_id,
                            "action": "new_business_location",
                            "list_id": selected.list_id,
                        })
                        continue
                    if record.location:
                        # Said in terms of what is on the screen below it. The
                        # old wording told people to choose an existing
                        # location even when none had been found, which reads
                        # as the app hiding the answer rather than not having
                        # one.
                        if location_matches:
                            reason = (
                                f"More than one QuickBooks customer is recorded at "
                                f"'{record.location}'. Pick the right one."
                            )
                        elif location_candidates:
                            reason = (
                                f"No QuickBooks customer is recorded at '{record.location}'. "
                                f"The ones below have similar names - pick one if it is the "
                                f"same business, or create a new customer."
                            )
                        else:
                            reason = (
                                f"This customer is not in QuickBooks. Nothing there matches the "
                                f"name or the location '{record.location}', so creating it is "
                                f"the only option here."
                            )
                        ambiguous.append({
                            "source_id": record.source_id,
                            "customer_name": record.customer_name,
                            "policy_number": record.invoice_number,
                            "job_name": record.job_name,
                            "reason": reason,
                            "candidates": [
                                {"list_id": item.list_id, "full_name": item.full_name}
                                for item in location_candidates
                            ],
                        })
                        continue
                    exact = gateway.find_customer(record.quickbooks_customer_name)
                    if exact is not None:
                        resolved.append(record.source_id)
                        continue
                    reason = ""   # worded below, once it is known what was found

                key = record.quickbooks_customer_name.casefold()
                if key not in candidate_cache:
                    candidate_cache[key] = list(gateway.customer_candidates(record.quickbooks_customer_name))
                candidates = candidate_cache[key]
                if not reason:
                    reason = (
                        "This exact name is not in QuickBooks. The customers below have "
                        "similar names - pick one if it is the same business, or create a "
                        "new customer."
                        if candidates else
                        "This customer is not in QuickBooks, and nothing there has a similar "
                        "name, so creating it is the only option here."
                    )
                ambiguous.append({
                    "source_id": record.source_id,
                    "customer_name": record.customer_name,
                    "policy_number": record.invoice_number,
                    "job_name": record.job_name,
                    "reason": reason,
                    "candidates": [
                        {"list_id": item.list_id, "full_name": item.full_name}
                        for item in candidates
                    ],
                })

        unknown = [source_id for source_id in source_ids if source_id not in self.records]
        return {
            "resolved_ids": resolved,
            "automatic_choices": automatic,
            "ambiguous": ambiguous,
            "unknown_ids": unknown,
        }

    def _new_business_location_customers(
        self, gateway, record: WorkRecord, candidate_cache: dict[str, list[object]]
    ) -> tuple[list[object], list[object]]:
        """Return New Business candidates and only the ones safe to auto-select.

        A sheet location can be longer than the old QuickBooks customer name
        ("Dpeg Retail LLC 1903 Hamilton Rd" vs "DPEG RETAIL LLC LOC 1903").
        Requiring the corporation words plus a distinctive location token
        prevents the plain parent name from being mistaken for a location.
        """
        if "new business" not in record.source_tab.casefold() or not record.location:
            return [], []
        # Look the company up, not the address. Searching QuickBooks for
        # "2406 East Expressway 83" asks it for customers with 2406 in their
        # name, which finds nothing and leaves the picker with no accounts to
        # offer - when every one of that company's locations was there to be
        # found under its own name.
        key = f"company:{record.quickbooks_customer_name.casefold()}"
        if key not in candidate_cache:
            candidate_cache[key] = list(
                gateway.customer_candidates(record.quickbooks_customer_name)
            )
        candidates = candidate_cache[key]
        corporation = _location_words(record.customer_name) - _LOCATION_FILLERS
        location = _location_words(record.location) - _LOCATION_FILLERS
        required_corporation_words = min(2, len(corporation))
        matches: list[object] = []
        for candidate in candidates:
            candidate_words = _location_words(candidate.full_name) - _LOCATION_FILLERS
            distinguishing = candidate_words - corporation
            if not distinguishing:
                continue  # the bare corporation is never a location match
            numbers = {word for word in distinguishing if word.isdigit()}
            enough_company_words = len(corporation & candidate_words) >= required_corporation_words
            if not enough_company_words:
                continue
            if numbers:
                if numbers <= location:
                    matches.append(candidate)
                continue
            if distinguishing <= location:
                matches.append(candidate)
        return candidates, matches

    def _refresh_resolution_fields(self, source_ids: list[str]) -> None:
        """Bring saved mappings and Renewal's old policy into the loaded rows."""
        labels = {
            self._tab_label(self.records[source_id].source_tab)
            for source_id in source_ids if source_id in self.records
        }
        for label in labels:
            index = self._row_lookup(label, force=True)
            for source_id in source_ids:
                record = self.records.get(source_id)
                if record is None or self._tab_label(record.source_tab) != label:
                    continue
                found = index.get(_policy_key(record))
                values = found[1] if found else {}
                for field in RESOLUTION_FIELDS:
                    if field in values:
                        self._remember(source_id, field, values[field])

    def _old_policy_customers(self, gateway, record: WorkRecord) -> tuple[list[object], bool]:
        """Top-level customers owning invoices for a Renewal's old policy."""
        if "renew" not in record.source_tab.casefold() or not record.old_policy_number:
            return [], False
        old = record.old_policy_number.strip()
        # Historical invoices in this company keep the full policy number
        # (for example CFP4802613-2025). Search that exact value first. The
        # app's older 11-character form is only a fallback for invoices that
        # were created by an older workflow and carry the full value in Memo.
        invoices = gateway.find_invoices(old)
        shortened_fallback = False
        if not invoices and len(old) > 11:
            invoices = gateway.find_invoices(old[:11])
            shortened_fallback = bool(invoices)
        parents: dict[str, object] = {}
        exact = True
        for invoice in invoices:
            if not invoice.customer_list_id:
                continue
            if shortened_fallback and _policy_number_key(old) not in _policy_number_key(invoice.memo):
                exact = False
            target = gateway.find_customer_by_id(invoice.customer_list_id)
            if target is None:
                continue
            parent = target
            if target.parent_list_id:
                parent = gateway.find_customer_by_id(target.parent_list_id) or target
            elif ":" in target.full_name:
                parent_name = target.full_name.split(":", 1)[0]
                parent = gateway.find_customer(parent_name) or target
            if ":" not in parent.full_name:
                parents[parent.list_id] = parent
        return list(parents.values()), exact

    def save_customer_mappings(self, choices: list[dict[str, str]]) -> dict[str, object]:
        """Verify dashboard choices, save them in memory, then write hidden sheet cells."""
        if not choices:
            raise ValueError("Choose at least one customer location.")

        factory = self._read_gateway if self.config.mode == "quickbooks" else self._gateway
        verified: list[tuple[WorkRecord, str, str, str]] = []
        with factory() as gateway:
            for choice in choices:
                source_id = str(choice.get("source_id", "")).strip()
                action = str(choice.get("action", "")).strip().casefold()
                record = self.records.get(source_id)
                if record is None:
                    raise ValueError("One selected policy is no longer loaded. Refresh and try again.")
                if action == "create":
                    verified.append((record, "", "", "create"))
                    continue
                list_id = str(choice.get("list_id", "")).strip()
                selected = gateway.find_customer_by_id(list_id) if list_id else None
                if selected is None or ":" in selected.full_name:
                    raise ValueError(f"The chosen customer for '{record.customer_name}' was not found.")
                saved_action = (
                    f"old_policy:{_policy_number_key(record.old_policy_number)}"
                    if action == "old_policy"
                    else "use_existing"
                )
                verified.append((record, selected.full_name, selected.list_id, saved_action))

        sheet_saved = True
        warning = ""
        updates: list[tuple[str, str]] = []
        try:
            for record, target, list_id, action in verified:
                label = self._tab_label(record.source_tab)
                columns = self._ensure_mapping_columns(label)
                row = self._sheet_row(record)
                title = self.config.google_sheet.tab_titles.get(label, "")
                if row < 1 or not title:
                    raise SheetsError(
                        f"The row for '{record.customer_name}' could not be identified in the sheet."
                    )
                values = {
                    "qb_customer_target": target,
                    "qb_customer_id": list_id,
                    "qb_customer_action": action,
                }
                for field, value in values.items():
                    updates.append((f"'{title}'!{column_letter(columns[field])}{row}", value))
            if updates:
                self._sheets().write(updates)
        except SheetsError as exc:
            sheet_saved = False
            warning = str(exc)
            self.sheet_write_error = warning
        else:
            self.sheet_write_error = ""

        # Keep the current session usable even if Google is briefly unavailable.
        # The warning is returned prominently; nothing is pretended to be saved there.
        with self._lock:
            for record, target, list_id, action in verified:
                current = self.records.get(record.source_id)
                if current is None:
                    continue
                extra = dict(current.extra)
                extra.update({
                    "qb_customer_target": target,
                    "qb_customer_id": list_id,
                    "qb_customer_action": action,
                })
                self.records[record.source_id] = replace(current, extra=tuple(extra.items()))
        return {
            "saved": [record.source_id for record, *_ in verified],
            "sheet_saved": sheet_saved,
            "warning": warning,
            "records": self.records_payload(),
        }

    def _ensure_mapping_columns(self, label: str) -> dict[str, int]:
        """Find or append the three hidden mapping columns for one tab."""
        settings = self.config.google_sheet
        if not (settings.enabled and settings.write_status):
            raise SheetsError("Writing customer choices back to Google Sheets is switched off.")
        title = settings.tab_titles.get(label, "")
        if not title:
            raise SheetsError("The policy's Google Sheet tab is not configured.")
        grid = self._sheets().read(title)
        columns = sheet_columns(grid)
        missing = [field for field in MAPPING_FIELDS if field not in columns]
        if missing:
            header_row = dashboard_header_row(grid)
            header = list(grid[header_row - 1]) if len(grid) >= header_row else []
            next_column = len(header)
            header_updates: list[tuple[str, str]] = []
            added: list[int] = []
            for field in missing:
                columns[field] = next_column
                added.append(next_column)
                header_updates.append(
                    (f"'{title}'!{column_letter(next_column)}{header_row}", MAPPING_HEADERS[field])
                )
                next_column += 1
            # A values write cannot extend a Google Sheet grid. Grow this tab
            # first by exactly the number of columns the mapping requires.
            self._sheets().ensure_column_count(title, next_column)
            self._sheets().write(header_updates)
            try:
                self._sheets().hide_columns(title, added)
            except SheetsError as exc:
                # The values are more important than presentation. Report the
                # hide problem, but do not throw away a verified selection.
                self.sheet_write_error = str(exc)
        self._status_columns[label] = {**self._status_columns.get(label, {}), **columns}
        return columns

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
        """Write what this machine has done into the sheet, for every loaded row.

        Nothing calls this on a timer. It is kept for putting a sheet back in
        step deliberately - after the columns have been cleared, say - because
        running it in the background meant cells changed while somebody was
        looking at them, which is indistinguishable from the app arguing with
        whoever typed them.
        """
        settings = self.config.google_sheet
        if not (settings.enabled and settings.write_status):
            return {"written": 0, "reason": "Writing to the sheet is switched off."}

        with self._lock:
            states = self.store.states_for(list(self.records), self.config.mode)
            invoices = self.store.invoice_states(list(self.records), self.config.mode)
            rows = list(self.records.values())

        updates: list[tuple[str, str]] = []
        noted: list[tuple[str, str, str]] = []
        unplaced = 0
        for record in rows:
            state = states.get(record.source_id)
            if _is_obsolete_name_limit_review(state):
                state = None
            marks = invoices.get(record.source_id, {})
            shown = self._state_payload(record, state, marks)
            held = dict(record.extra)
            for field, column in self._SHEET_FIELDS:
                word = self._SHEET_WORD.get(str(shown.get(field, "")), "")
                if not word or str(held.get(column, "")).strip() == word:
                    continue                      # nothing to say, or already says it
                # "Not needed" goes in only when somebody decided it. The same
                # word is also reached by a row having nothing to bill - no
                # commission, no fee - and that is a reading of the row, not a
                # decision. Written to the sheet it becomes permanent: fill the
                # commission in afterwards and the cell still says Not needed,
                # for ever, because the sheet is believed over the row.
                if word == "Not needed":
                    kind = field.removesuffix("_status")
                    decided = marks.get(kind)
                    if decided is None or decided.status != "not_needed":
                        continue
                # Every status, not only the ones that record an action: the
                # sheet is meant to show what the dashboard shows, so somebody
                # reading it sees Needs review on a row that is not ready
                # rather than a bare Pending that says nothing about why.
                #
                # The cost is real and worth stating. Typing Pending on a row
                # the app considers incomplete will read Needs review again
                # shortly - not the app being stubborn, but the row being
                # described. The reset still happened; the word just reports
                # what the row is now.
                cell = self._cell_for(record, column)
                if not cell:
                    # Its row in the sheet could not be identified - usually
                    # two rows that look the same to the naked eye, where
                    # writing to either would be a guess. Counted and reported
                    # rather than passed over in silence: the person would
                    # otherwise watch a status never appear and have no idea why.
                    unplaced += 1
                    continue
                updates.append((cell, word))
                noted.append((record.source_id, column, word))
            if len(updates) >= limit:
                break

        written = 0
        try:
            # Google counts requests, not cells, so this goes in as few as possible.
            for start in range(0, len(updates), 500):
                written += self._sheets().write(updates[start:start + 500])
        except SheetsError as exc:
            # Nothing is noted as written, so memory keeps matching the sheet
            # and a later read cannot mistake the old word for a fresh edit.
            self.sheet_write_error = str(exc)
            return {"written": written, "error": str(exc)}
        for source_id, column, word in noted:
            self._remember(source_id, column, word)
        self._wrote((source_id, column) for source_id, column, _ in noted)
        self.sheet_write_error = ""
        return {"written": written, "rows": len(rows), "unplaced": unplaced}

    def _held(self, source_id: str, column: str) -> str:
        """What this machine believes the sheet says in one status cell."""
        record = self.records.get(source_id)
        return str(dict(record.extra).get(column, "")).strip() if record else ""

    def _wrote(self, cells) -> None:
        """Stamp cells as just written, once the sheet has confirmed the write."""
        now = monotonic()
        for key in cells:
            self._written_at[key] = now

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
        label = self._tab_label(record.source_tab)
        index = self._status_columns.get(label, {}).get(column)
        title = self.config.google_sheet.tab_titles.get(label, "")
        if row < 1 or index is None or not title:
            return ""
        return f"'{title}'!{column_letter(index)}{row}"

    def _publish_status(self, results: list[dict[str, object]]) -> None:
        """Write each result into its row's status column in the sheet."""
        settings = self.config.google_sheet
        if not (settings.enabled and settings.write_status and results):
            return
        updates: list[tuple[str, str]] = []
        touched: list[tuple[str, str, str]] = []     # (policy, column, word before)
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
                    touched.append((source_id, column, self._held(source_id, column)))
                    self._remember(source_id, column, word)
        if not updates:
            return
        try:
            self._sheets().write(updates)
        except SheetsError as exc:
            self.sheet_write_error = str(exc)
            # The sheet still says what it said. Believing otherwise is how a
            # failed write turned into a reset: the next read saw the old
            # "Pending", took it for somebody typing it, and wiped the work.
            for source_id, column, before in touched:
                self._remember(source_id, column, before)
        else:
            self.sheet_write_error = ""
            self._wrote((source_id, column) for source_id, column, _ in touched)

    def _status_cell(self, source_id: str, operation: str) -> str:
        """The A1 address of one row's status column, or "" if it cannot be found."""
        record = self.records.get(source_id)
        field = self._STATUS_FIELD.get(operation)
        if record is None or not field:
            return ""
        return self._cell_for(record, field)

    def _tab_label(self, source_tab: str) -> str:
        """Which configured tab a row's own tab name means.

        The rows arrive naming their tab the way the published link names it -
        "Renewal" - while the settings name the same tab "Renewal Business".
        Compared directly the two never match, and the effect is not a tab with
        no rows: it is every row on that tab becoming impossible to find in the
        sheet, so no status can be read from it or written to it. Matched
        exactly first, then ignoring case, then on the one thing that actually
        separates the two tabs - whether it is the renewal one.
        """
        titles = self.config.google_sheet.tab_titles
        if source_tab in titles:
            return source_tab
        wanted = source_tab.strip().casefold()
        if not wanted:
            return ""
        for label in titles:
            if label.strip().casefold() == wanted:
                return label
        renewal = "renew" in wanted
        for label in titles:
            if ("renew" in label.casefold()) == renewal:
                return label
        return ""

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
        found = self._row_lookup(self._tab_label(record.source_tab)).get(_policy_key(record))
        return found[0] if found else 0

    def honour_resets(self, targets: list[tuple[str, str]]) -> int:
        """Act on somebody typing Pending into the sheet.

        Each target is one row and one column that has *just changed* to
        Pending. It matters that this is an event and not a standing state: a
        cell that simply says Pending must not keep deleting the record of work
        done since, or a row could be sent, created in QuickBooks, and wiped
        from this machine's memory a few seconds later, over and over.

        What goes is this machine's memory of the work - the customer record
        that would make a send skip the row, or a mark on an invoice. The row
        is then free to say what it is, and the next send really does send.
        """
        cleared = 0
        for source_id, column in targets:
            if source_id not in self.records:
                continue
            if column == "qb_customer_job":
                if self.store.forget_record(source_id, self.config.mode):
                    cleared += 1
                continue
            kind = column.removeprefix("qb_")
            if kind in set(INVOICE_KINDS.values()):
                if self.store.forget_invoice(source_id, self.config.mode, kind):
                    cleared += 1
        return cleared

    def refresh_statuses(self) -> int:
        """Re-read just the four status columns, straight from the sheet.

        The published link carries them too, but it answers in a minute or
        three and fails often, so a status someone typed could sit unseen for
        that long. The sheet itself answers in a couple of seconds. The rows
        still come from the link - only what has been done to them is taken
        from here, which is the part that changes while people work.
        """
        settings = self.config.google_sheet
        if not settings.enabled or settings.read_rows:
            return 0        # reading the sheet directly is already this fresh
        with self._status_lock:
            return self._refresh_statuses()

    def _refresh_statuses(self) -> int:
        settings = self.config.google_sheet
        changed = 0
        # Cells that have only now become Pending. A reset is the moment of
        # the change, not the word sitting there afterwards.
        newly_reset: list[tuple[str, str]] = []
        for label in settings.tab_titles:
            # Taken before asking, not after: what matters is whether the sheet
            # could have answered from before one of our own writes landed.
            asked_at = monotonic()
            index = self._row_lookup(label, force=True)
            if not index:
                continue
            for record in list(self.records.values()):
                if self._tab_label(record.source_tab) != label:
                    continue
                found = index.get(_policy_key(record))
                said = found[1] if found else {}
                for column, word in said.items():
                    if str(dict(record.extra).get(column, "")).strip() == word:
                        continue
                    # This machine wrote this cell after the read began, so
                    # the answer may predate the write. Believing it is how a
                    # premium, created seconds earlier, was read back as the
                    # old "Pending", taken for a reset and wiped. The next
                    # read, begun after the write, settles it.
                    if self._written_at.get((record.source_id, column), 0.0) >= asked_at:
                        continue
                    self._remember(record.source_id, column, word)
                    changed += 1
                    if self._SHEET_STATUS.get(word.strip().casefold()) == _RESET:
                        newly_reset.append((record.source_id, column))
        if newly_reset:
            self.honour_resets(newly_reset)
        # Now the other direction, in the same pass: a row this machine has
        # really done, whose cell in the sheet still says otherwise.
        changed += self._push_settled()
        if changed:
            # The page only pulls the rows again when this moves. Without it
            # the app knew a colleague's status within the minute but the
            # screen kept the old one until somebody pressed Refresh.
            self.revision += 1
        return changed

    def _push_settled(self) -> int:
        """Tell the sheet about work this machine has actually done.

        Only settled facts travel this way: Created, Failed, and a Not needed
        somebody chose. Never Pending, and never Needs review - those are the
        app's reading of a row rather than a record of anything happening, and
        writing them back every quarter minute would be the app arguing with
        whoever is typing in the sheet. The deliberate Sync button still writes
        the full picture; this only stops a row that was created in QuickBooks
        from sitting in the sheet as Pending for ever.
        """
        settings = self.config.google_sheet
        if not (settings.enabled and settings.write_status):
            return 0
        with self._lock:
            states = self.store.states_for(list(self.records), self.config.mode)
            invoices = self.store.invoice_states(list(self.records), self.config.mode)
            rows = list(self.records.values())

        updates: list[tuple[str, str]] = []
        noted: list[tuple[str, str, str]] = []
        for record in rows:
            state = states.get(record.source_id)
            if _is_obsolete_name_limit_review(state):
                state = None
            marks = invoices.get(record.source_id, {})
            shown = self._state_payload(record, state, marks)
            held = dict(record.extra)
            for field, column in self._SHEET_FIELDS:
                word = self._SHEET_WORD.get(str(shown.get(field, "")), "")
                if word not in _SETTLED or str(held.get(column, "")).strip() == word:
                    continue
                if word == "Not needed":
                    decided = marks.get(field.removesuffix("_status"))
                    if decided is None or decided.status != "not_needed":
                        continue        # nothing to bill is a reading, not a decision
                cell = self._cell_for(record, column)
                if not cell:
                    continue            # two rows look alike; sync_sheet reports it
                updates.append((cell, word))
                noted.append((record.source_id, column, word))
        if not updates:
            return 0
        try:
            self._sheets().write(updates)
        except SheetsError as exc:
            # Nothing is noted as written: the next pass must try these again
            # rather than believe the sheet says what it does not.
            self.sheet_write_error = str(exc)
            return 0
        self.sheet_write_error = ""
        for source_id, column, word in noted:
            self._remember(source_id, column, word)
        self._wrote((source_id, column) for source_id, column, _ in noted)
        return len(updates)

    def _row_lookup(
        self, label: str, force: bool = False
    ) -> dict[tuple[str, str, str], tuple[int, dict[str, str]]]:
        """Policy -> (real sheet row, its four statuses), for one tab.

        One read of the tab answers both questions, so the statuses come along
        for free with the row numbers.
        """
        fresh = (monotonic() - self._lookup_at) < LOOKUP_SECONDS
        if not force and fresh and label in self._row_index:
            return self._row_index[label]
        title = self.config.google_sheet.tab_titles.get(label, "")
        if not title:
            return {}
        try:
            grid = self._sheets().read(title)
        except SheetsError as exc:
            self.sheet_write_error = str(exc)
            return {}
        self._status_columns[label] = sheet_columns(grid)
        try:
            # Every row, ready or not: this index is how a policy is found in
            # the sheet, and the published link's idea of which rows are
            # workable need not match ours. A row it sends that we would have
            # filtered out still has to be findable, or its status can never
            # be read or written.
            found = build_records(label, grid, every_row=True)
        except WebSourceError:
            return {}
        seen: dict[tuple[str, str, str], tuple[int, dict[str, str]]] = {}
        ambiguous: set[tuple[str, str, str]] = set()
        for entry in found.records:
            key = _policy_key(entry)
            if key in seen:
                ambiguous.add(key)
            held = dict(entry.extra)
            seen[key] = (
                entry.row_number,
                {
                    field: str(held.get(field, "")).strip()
                    for field in (*STATUS_FIELDS, *RESOLUTION_FIELDS)
                },
            )
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
                company_file = str(Path(company_file.strip()).resolve())
                if not Path(company_file).is_file():
                    # A typed path fails here as often for being unreachable as
                    # for being wrong, and the two need different fixes.
                    raise ValueError(
                        f"No company file could be read at {company_file}. Check the "
                        f"spelling, and - if it is on another computer - that the "
                        f"folder is shared and this computer can reach it."
                    )
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

# The same idea in the sheet's own words: what the app puts there unasked.
# Anything else it might think about a row is left for the row to say.
_ACTIONS = {"Created", "Failed", "Not needed"}


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


def _policy_number_key(value: str) -> str:
    """Compare policy numbers without formatting differences such as spaces or hyphens."""
    return "".join(character for character in value.casefold() if character.isalnum())


def _key(path: str) -> str:
    """One spelling per file, so the same company is never listed twice."""
    if not path.strip():
        return ""
    try:
        return str(Path(path).resolve()).casefold()
    except OSError:
        return path.strip().casefold()


def _same_file(left: str, right: str) -> bool:
    """Two paths pointing at one file, whatever the spelling or capitals.

    The same test the session guard uses, so picking the company a second
    machine reaches by a mapped drive is understood as the company already in
    use - and keeps the history this app has recorded for it - rather than
    being treated as a new company and starting again from nothing.
    """
    return same_company_file(left, right)


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
