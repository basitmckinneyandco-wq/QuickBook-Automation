"""Glue: holds the loaded spreadsheet, the history, and the processor together."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from threading import Lock, Thread
from time import monotonic, sleep
from typing import Mapping

from .config import AppConfig, database_for, load_config, save_company, save_data_source
from .edits import EditError, apply, apply_all
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
        self._cache: dict[str, list[list[object]]] | None = None
        self._cached_at = 0.0
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
        """The customer status and each invoice's status, side by side."""
        item: dict[str, object] = {}
        if state is not None and state.status:
            item["status"] = state.status
            item["customer_list_id"] = state.customer_list_id
            item["job_list_id"] = state.job_list_id
            item["invoice_txn_id"] = state.invoice_txn_id
            item["error_message"] = state.error_message
        else:
            item["status"] = "review" if record.review_reasons() else "ready"

        held = invoices or {}
        for kind in INVOICE_KINDS.values():
            saved = held.get(kind)
            if saved is not None and saved.status:
                item[f"{kind}_status"] = saved.status
                item[f"{kind}_txn_id"] = saved.txn_id
                item[f"{kind}_ref_number"] = saved.ref_number
                item[f"{kind}_error"] = saved.error_message
            else:
                item[f"{kind}_status"] = (
                    "review" if record.invoice_reasons(kind) else "ready"
                )
        return item

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
            sleep(WATCH_SECONDS)
            if self.loading:
                continue
            try:
                before = self.revision
                self.fetch_web(force=True)
                if self.revision != before:
                    print(
                        f"Sheet changed - now {len(self.records)} policies.", flush=True
                    )
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
        if not self.config.data_source.enabled:
            raise WebSourceError("No data source URL is set in config.json.")
        with self._lock:
            tabs = self._tabs(force)
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
        fresh = self._cache is not None and (monotonic() - self._cached_at) < CACHE_SECONDS
        if force or not fresh:
            self._cache = fetch_tabs(self.config.data_source.url)
            self._cached_at = monotonic()
        assert self._cache is not None
        return self._cache

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
            return results + unknown
        finally:
            self._lock.release()

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
