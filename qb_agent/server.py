"""A tiny local web server: serves the dashboard and a handful of JSON endpoints.

It listens on 127.0.0.1 only, so nothing outside this computer can reach it.
Policies come from the Google Sheet feed, so every endpoint here is JSON.
"""

from __future__ import annotations

import json
import webbrowser
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Full, Queue
from threading import Thread, Timer
from typing import Any

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


def log(message: str) -> None:
    """Queue a line for the console; drop it rather than ever block a caller."""
    try:
        _LOG.put_nowait(message)
    except Full:
        pass


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
            elif self.path == "/api/edit":
                self._json(self._edit())
            elif self.path == "/api/edit-many":
                self._json(self._edit_many())
            elif self.path == "/api/process":
                self._json(self._process())
            elif self.path == "/api/company":
                self._json(self._company())
            elif self.path == "/api/source":
                self._json(self._source())
            elif self.path == "/api/invoice-needed":
                self._json(self._invoice_needed())
            else:
                self._json({"error": "Unknown endpoint."}, HTTPStatus.NOT_FOUND)
        except (WebSourceError, EditError, ValueError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # never let the dashboard hang on a traceback
            self._json({"error": f"Unexpected server error: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    # --- endpoints -----------------------------------------------------------

    def _fetch(self) -> dict[str, object]:
        payload = self._read_json() if int(self.headers.get("Content-Length", "0")) > 0 else {}
        summary = self.server.runtime.fetch_web(
            tab=str(payload.get("tab", "")), force=bool(payload.get("force", False))
        )
        log(
            f"Fetched {summary['loaded_rows']} row(s) from the {summary['tab']} tab "
            f"({summary['skipped_rows']} skipped)"
        )
        return {"import": summary, "records": self.server.runtime.records_payload()}

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
        return {"company": switched, "health": self.server.runtime.health()}

    def _source(self) -> dict[str, object]:
        """Change the published-sheet link the rows are read from."""
        payload = self._read_json()
        changed = self.server.runtime.use_data_source(str(payload.get("url", "")))
        log(f"Data source changed, {changed['rows']} rows loaded")
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
        results = self.server.runtime.process([str(value) for value in ids], operation)
        log(f"Processed {len(results)} policy record(s) [{operation.value}]:")
        for result in results:
            log(f"  [{result.get('status')}] {result.get('label')} - {result.get('message')}")
        return {
            "results": results,
            "records": self.server.runtime.records_payload(),
            "activity": self.server.runtime.activity(),
        }

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
    runtime.start_prefetch()  # begin downloading the sheet while the page loads
    runtime.start_watching()  # then keep re-reading it, so new rows turn up on their own
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
