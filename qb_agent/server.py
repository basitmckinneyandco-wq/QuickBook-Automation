"""A tiny local web server: serves the dashboard and a handful of JSON endpoints.

It listens on 127.0.0.1 only, so nothing outside this computer can reach it.
Policies come from the Google Sheet feed, so every endpoint here is JSON.
"""

from __future__ import annotations

import json
import re
from collections import Counter
import webbrowser
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Full, Queue
from threading import Event, Thread, Timer
from typing import Any

from .activity_log import ActivityLog
from .edits import EditError
from .models import Operation
from .runtime import Runtime
from .websource import WebSourceError

# Printing goes through a background thread, because on Windows a console left
# in selection mode (click in the window and it stops redrawing) blocks every
# write until a key is pressed. A request thread that blocks there has already
# done its work - written the invoice, saved the row - but never replies, and
# the dashboard sits on a spinner. Handlers must never wait on the terminal.
_LOG: "Queue[str]" = Queue(maxsize=500)
_ACTIVITY_LOGGER: ActivityLog | None = None
ACTIVITY_LOG_SPREADSHEET_ID = "1bjUM0ggJKLSYsgpCF9zYzBE83LOCN8inj0pXJTNH8WA"


def log(message: str) -> None:
    """Queue a line for the console; drop it rather than ever block a caller."""
    try:
        _LOG.put_nowait(message)
    except Full:
        pass


def shared_activity(
    event: str,
    details: str = "",
    *,
    company: str = "",
    policy_number: str = "",
) -> None:
    """Send a limited operational event to the optional cloud activity sheet."""
    logger = _ACTIVITY_LOGGER
    if logger is not None:
        logger.record(event, details, company=company, policy_number=policy_number)


def _activity_title(operation: str) -> str:
    return {
        "create_invoice": "Invoice",
        "premium_invoice": "Premium invoice",
        "commission_invoice": "Commission invoice",
        "fees_invoice": "Fees invoice",
        "create_bill": "Bill",
        "create_customer": "Customer/job",
        "update_customer": "Customer update",
    }.get(operation, "QuickBooks action")


def _activity_event(operation: str, status: str, message: str, dry_run: bool = False) -> str:
    title = _activity_title(operation)
    state = status.casefold()
    if state == "failed":
        return f"{title} failed"
    if state == "needs_review":
        return f"{title} needs review"
    if state == "already_exists":
        return f"{title} already exists"
    if state == "not_needed":
        return f"{title} not needed"
    if state == "completed":
        if dry_run:
            return f"{title} ready (test mode)"
        return f"{title} updated" if "updated" in message.casefold() else f"{title} created"
    return f"{title} {state or 'finished'}"


def _safe_activity_details(message: object, record: object | None) -> str:
    """Keep useful result/error text while omitting customer/job identifiers."""
    detail = str(message or "")
    if record is not None:
        private_names = {
            str(getattr(record, "customer_name", "") or ""),
            str(getattr(record, "quickbooks_customer_name", "") or ""),
            str(getattr(record, "quickbooks_job_name", "") or ""),
        }
        for private_name in sorted(private_names, key=len, reverse=True):
            if private_name:
                detail = re.sub(re.escape(private_name), "[customer/job]", detail, flags=re.IGNORECASE)
    return detail[:1000]


def _watch_policy_progress(runtime: Runtime, operation: Operation, stop: Event) -> None:
    """Log the policy number currently being processed without exposing its row."""
    company = runtime.config.company.display_name
    seen: set[str] = set()
    title = _activity_title(operation.value)
    while not stop.wait(0.5):
        progress = runtime.progress()
        source_id = str(progress.get("current") or "")
        if not progress.get("active") or not source_id or source_id in seen:
            continue
        seen.add(source_id)
        record = runtime.records.get(source_id)
        if record is not None:
            shared_activity(
                f"{title} in progress",
                "QuickBooks is processing this policy.",
                company=company,
                policy_number=record.invoice_number,
            )


def _drain_log() -> None:
    while True:
        line = _LOG.get()
        try:
            print(line, flush=True)
        except Exception:
            pass   # a closed or broken console must not kill the printer


Thread(target=_drain_log, daemon=True).start()

# The largest JSON body any endpoint accepts. A batch of policy ids is small;
# this is only here so a malformed request cannot make the server read for ever.
MAX_BODY_BYTES = 2 * 1024 * 1024


class DashboardServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], runtime: Runtime, static_dir: Path):
        self.runtime = runtime
        self.static_dir = static_dir
        super().__init__(address, DashboardHandler)


class DashboardHandler(SimpleHTTPRequestHandler):
    server: DashboardServer

    def translate_path(self, path: str) -> str:
        """Serve static files out of the web/ folder rather than the working directory."""
        original = self.directory
        self.directory = str(self.server.static_dir)
        try:
            return super().translate_path(path)
        finally:
            self.directory = original

    def do_GET(self) -> None:
        runtime = self.server.runtime
        if self.path == "/api/health":
            self._json(runtime.health())
        elif self.path == "/api/records":
            self._json({"records": runtime.records_payload(), "health": runtime.health()})
        elif self.path == "/api/activity":
            self._json({"activity": runtime.activity()})
        elif self.path == "/api/live-activity":
            logger = _ACTIVITY_LOGGER
            self._json(logger.latest() if logger is not None else {"enabled": False, "rows": []})
        elif self.path == "/api/progress":
            # Deliberately lock-free: this is read while a batch holds the lock.
            self._json(runtime.progress())
        elif self.path == "/api/preflight":
            self._json(runtime.preflight())
        elif self.path == "/api/companies":
            self._json(runtime.companies())
        elif self.path.startswith("/api/"):
            self._json({"error": "Unknown endpoint."}, HTTPStatus.NOT_FOUND)
        else:
            super().do_GET()

    def do_POST(self) -> None:
        try:
            if self.path == "/api/fetch":
                self._json(self._fetch())
            elif self.path == "/api/statuses":
                self._json(self._statuses())
            elif self.path == "/api/edit":
                self._json(self._edit())
            elif self.path == "/api/edit-many":
                self._json(self._edit_many())
            elif self.path == "/api/process":
                self._json(self._process())
            elif self.path == "/api/customer-resolutions":
                self._json(self._customer_resolutions())
            elif self.path == "/api/bill-vendor-resolutions":
                self._json(self._bill_vendor_resolutions())
            elif self.path == "/api/customer-mappings":
                self._json(self._customer_mappings())
            elif self.path == "/api/company":
                self._json(self._company())
            elif self.path == "/api/source":
                self._json(self._source())
            elif self.path == "/api/invoice-needed":
                self._json(self._invoice_needed())
            else:
                self._json({"error": "Unknown endpoint."}, HTTPStatus.NOT_FOUND)
        except (WebSourceError, EditError, ValueError, json.JSONDecodeError) as exc:
            shared_activity(
                "Request refused", str(exc)[:1000],
                company=self.server.runtime.config.company.display_name,
            )
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # never let the dashboard hang on a traceback
            shared_activity(
                "Application error", str(exc)[:1000],
                company=self.server.runtime.config.company.display_name,
            )
            self._json({"error": f"Unexpected server error: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    # --- endpoints -----------------------------------------------------------

    def _fetch(self) -> dict[str, object]:
        payload = self._read_json() if int(self.headers.get("Content-Length", "0")) > 0 else {}
        summary = self.server.runtime.fetch_web(
            tab=str(payload.get("tab", "")), force=bool(payload.get("force", False))
        )
        # The link's copy of the status columns is minutes old by the time it
        # arrives. Read them again straight from the sheet, or a refresh would
        # show the page older statuses than it had before the click.
        self.server.runtime.refresh_statuses()
        log(
            f"Fetched {summary['loaded_rows']} row(s) from the {summary['tab']} tab "
            f"({summary['skipped_rows']} skipped)"
        )
        shared_activity(
            "Policies refreshed",
            f"{summary['loaded_rows']} rows loaded; {summary['skipped_rows']} skipped.",
            company=self.server.runtime.config.company.display_name,
        )
        return {"import": summary, "records": self.server.runtime.records_payload()}

    def _statuses(self) -> dict[str, object]:
        """Re-read the four status columns now - a couple of seconds, not minutes.

        Pressing Refresh is someone asking to see what colleagues have done.
        The rows themselves come through the published link, which is slow;
        this answers the part that changes while people work, straight away.
        """
        changed = self.server.runtime.refresh_statuses()
        return {"changed": changed, "sheet_enabled": self.server.runtime.config.google_sheet.enabled}

    def _edit_many(self) -> dict[str, object]:
        """Apply the same corrections to every chosen policy."""
        payload = self._read_json()
        ids = payload.get("ids")
        values = payload.get("values")
        if not isinstance(ids, list) or not ids:
            raise ValueError("A bulk edit needs at least one policy.")
        if not isinstance(values, dict) or not values:
            raise ValueError("A bulk edit needs at least one column.")
        result = self.server.runtime.edit_many(
            [str(one) for one in ids], {str(k): str(v) for k, v in values.items()}
        )
        log(f"Bulk edit: {len(result['updated'])} rows, {len(result['failed'])} refused")
        shared_activity(
            "Bulk edit",
            f"{len(result['updated'])} rows updated; {len(result['failed'])} refused.",
            company=self.server.runtime.config.company.display_name,
        )
        return result

    def _edit(self) -> dict[str, object]:
        payload = self._read_json()
        source_id = str(payload.get("source_id", "")).strip()
        if not source_id:
            raise ValueError("An edit needs a policy.")

        values = payload.get("values")
        if not isinstance(values, dict):
            # Single-field form: {"field": ..., "value": ...}
            field = str(payload.get("field", "")).strip()
            if not field:
                raise ValueError("An edit needs at least one column.")
            values = {field: payload.get("value", "")}
        if not values:
            raise ValueError("Nothing was changed.")
        if len(values) > 40:
            raise ValueError("Too many columns in one edit.")

        cleaned = {str(field): str(value) for field, value in values.items()}
        return {"record": self.server.runtime.edit(source_id, cleaned)}

    def _company(self) -> dict[str, object]:
        """Adopt whichever company QuickBooks has open, or a named file."""
        from .quickbooks import QuickBooksError

        payload = self._read_json() if int(self.headers.get("Content-Length", "0")) > 0 else {}
        try:
            switched = self.server.runtime.use_company(str(payload.get("file_path", "")))
        except QuickBooksError as exc:
            raise ValueError(str(exc)) from exc
        log(f"Now pointed at {switched['company']} ({switched['file_path']})")
        shared_activity(
            "QuickBooks company changed", "The selected company file changed.",
            company=self.server.runtime.config.company.display_name,
        )
        return {"company": switched, "health": self.server.runtime.health()}

    def _source(self) -> dict[str, object]:
        """Change the published-sheet link the rows are read from."""
        payload = self._read_json()
        changed = self.server.runtime.use_data_source(str(payload.get("url", "")))
        log(f"Data source changed, {changed['rows']} rows loaded")
        shared_activity(
            "Data source changed", f"{changed['rows']} rows loaded.",
            company=self.server.runtime.config.company.display_name,
        )
        return {"source": changed, "health": self.server.runtime.health()}

    def _invoice_needed(self) -> dict[str, object]:
        """Mark one kind of invoice as not needed for one policy, or wanted again."""
        payload = self._read_json()
        record = self.server.runtime.set_invoice_needed(
            str(payload.get("source_id", "")),
            str(payload.get("kind", "")),
            bool(payload.get("needed", True)),
        )
        return {"record": record}

    def _process(self) -> dict[str, object]:
        payload = self._read_json()
        ids = payload.get("ids", [])
        if not isinstance(ids, list) or not ids:
            raise ValueError("Select at least one policy first.")
        try:
            operation = Operation(str(payload.get("operation", Operation.CREATE_INVOICE.value)))
        except ValueError as exc:
            raise ValueError("That action is not supported.") from exc
        vendor_choices = payload.get("vendor_choices", {})
        if not isinstance(vendor_choices, dict):
            raise ValueError("Bill vendor choices are invalid.")
        cleaned_vendor_choices = {
            str(source_id): str(list_id)
            for source_id, list_id in vendor_choices.items()
        }
        runtime = self.server.runtime
        company = runtime.config.company.display_name
        shared_activity(
            "Work started", f"{len(ids)} record(s); action: {operation.value}.",
            company=company,
        )
        stop_progress = Event()
        progress_watcher = Thread(
            target=_watch_policy_progress,
            args=(runtime, operation, stop_progress),
            daemon=True,
        )
        progress_watcher.start()
        try:
            results = runtime.process(
                [str(value) for value in ids], operation, cleaned_vendor_choices
            )
        finally:
            stop_progress.set()
        for result in results:
            source_id = str(result.get("source_id", ""))
            record = runtime.records.get(source_id)
            outcome_operation = str(result.get("operation") or operation.value)
            message = str(result.get("message", ""))
            policy_number = (
                str(getattr(record, "invoice_number", "") or "")
                if record is not None
                else str(result.get("invoice_ref_number", "") or "")
            )
            status = str(result.get("status", ""))
            dry_run = bool(result.get("dry_run", False))
            details = _safe_activity_details(message, record)
            invoice_events: list[tuple[str, str]] = []
            steps = result.get("steps", [])
            if outcome_operation == Operation.CREATE_INVOICE.value and isinstance(steps, list):
                for step in steps:
                    premium_match = re.search(
                        r"^(Premium|Commission|Fees) invoice .+?\s(created|updated)\s+on\b",
                        str(step),
                        flags=re.IGNORECASE,
                    )
                    if premium_match:
                        invoice_events.append(
                            (f"{premium_match.group(1).title()} invoice {premium_match.group(2).lower()}",
                             "Prepared in test mode; not sent to QuickBooks."
                             if dry_run else "Transaction recorded in QuickBooks.")
                        )
                        continue
                    invoice_match = re.search(
                        r"^Invoice .+?\s(created|already existed)\b",
                        str(step),
                        flags=re.IGNORECASE,
                    )
                    if invoice_match:
                        verb = invoice_match.group(1).lower()
                        invoice_events.append(
                            ("Invoice already exists" if verb == "already existed" else "Invoice created",
                             "Prepared in test mode; not sent to QuickBooks."
                             if dry_run else "Transaction recorded in QuickBooks.")
                        )
            for event, event_details in invoice_events:
                shared_activity(
                    event,
                    event_details,
                    company=company,
                    policy_number=policy_number,
                )
            if not invoice_events or status.casefold() in {"failed", "needs_review"}:
                shared_activity(
                    _activity_event(outcome_operation, status, message, dry_run),
                    details,
                    company=company,
                    policy_number=policy_number,
                )
            if "warning" in message.casefold():
                shared_activity(
                    "Invoice warning",
                    details,
                    company=company,
                    policy_number=policy_number,
                )
        log(f"Processed {len(results)} policy record(s) [{operation.value}]:")
        for result in results:
            log(f"  [{result.get('status')}] {result.get('label')} - {result.get('message')}")
        counts = Counter(str(result.get("status", "unknown")) for result in results)
        summary = "; ".join(
            f"{count} {status.replace('_', ' ')}"
            for status, count in sorted(counts.items())
        )
        shared_activity(
            "Work finished",
            f"{len(results)} record(s) processed" + (f": {summary}." if summary else "."),
            company=company,
        )
        return {
            "results": results,
            "records": self.server.runtime.records_payload(),
            "activity": self.server.runtime.activity(),
        }

    def _customer_resolutions(self) -> dict[str, object]:
        payload = self._read_json()
        ids = payload.get("ids", [])
        if not isinstance(ids, list) or not ids:
            raise ValueError("Select at least one policy first.")
        return self.server.runtime.customer_resolutions([str(value) for value in ids])

    def _bill_vendor_resolutions(self) -> dict[str, object]:
        payload = self._read_json()
        ids = payload.get("ids", [])
        if not isinstance(ids, list) or not ids:
            raise ValueError("Select at least one policy first.")
        return self.server.runtime.bill_vendor_resolutions([str(value) for value in ids])

    def _customer_mappings(self) -> dict[str, object]:
        payload = self._read_json()
        choices = payload.get("choices", [])
        if not isinstance(choices, list) or not choices:
            raise ValueError("Choose at least one customer location.")
        cleaned: list[dict[str, str]] = []
        for choice in choices:
            if not isinstance(choice, dict):
                raise ValueError("A customer choice is invalid.")
            cleaned.append({str(key): str(value) for key, value in choice.items()})
        return self.server.runtime.save_customer_mappings(cleaned)

    # --- plumbing ------------------------------------------------------------

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("Invalid request body.")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object.")
        return value

    def _json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return  # keep the console clean; we print the things that matter ourselves


def serve(
    runtime: Runtime,
    host: str,
    port: int,
    static_dir: Path,
    *,
    open_browser: bool = False,
) -> None:
    try:
        server = DashboardServer((host, port), runtime, static_dir)
    except OSError as exc:
        # Almost always a second copy being started while the first still runs,
        # which Windows reports as WinError 10048 under a wall of traceback.
        if getattr(exc, "winerror", 0) == 10048 or exc.errno in (48, 98):
            print(
                f"\n  The dashboard is already running.\n"
                f"  Open it here:  http://{host}:{port}\n\n"
                f"  If you meant to restart it, close the other window first -\n"
                f"  the one with 'Press Ctrl+C to stop' in it.\n"
            )
            raise SystemExit(1) from None
        raise
    global _ACTIVITY_LOGGER
    _ACTIVITY_LOGGER = ActivityLog(
        runtime.config.google_sheet.key_file,
        ACTIVITY_LOG_SPREADSHEET_ID,
    )
    _ACTIVITY_LOGGER.start()
    shared_activity("Dashboard started", "Dashboard is online.")
    runtime.start_prefetch()  # begin downloading the sheet while the page loads
    runtime.start_watching()  # then keep re-reading it, so new rows turn up on their own
    runtime.start_status_watch()  # and the four status columns, far more often
    url = f"http://{host}:{port}"
    health = runtime.health()
    print(f"\n  QuickBooks automation dashboard: {url}")
    print(f"  Mode: {health['mode']}   Writes to QuickBooks: {'ON' if health['writes_enabled'] else 'OFF'}")
    if health["blocked_reason"]:
        print(f"  Safe mode because {health['blocked_reason']}")
    print("  Press Ctrl+C to stop.\n")
    if open_browser:
        Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        server.server_close()
