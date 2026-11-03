"""Optional shared activity logging through the existing Google Sheets service account.

Only coarse operational events are sent. Customer names, policy numbers,
QuickBooks messages, company paths, and field values stay in local History.
"""

from __future__ import annotations

from datetime import datetime, timezone
from getpass import getuser
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Lock, Thread
import socket
import urllib.parse

from .gsheets import Sheets

LOG_TAB = "App Activity Logs"
HEADERS = ("Timestamp (UTC)", "Computer", "Windows user", "Activity", "Details")
MAX_ROWS = 250


class ActivityLog:
    """Asynchronous writer plus a bounded reader for the separate live viewer."""

    def __init__(self, key_file: str, spreadsheet_id: str):
        self.key_file = str(key_file or "")
        self.spreadsheet_id = str(spreadsheet_id or "")
        self.enabled = bool(
            self.key_file and self.spreadsheet_id and Path(self.key_file).is_file()
        )
        self._sheets = Sheets(self.key_file, self.spreadsheet_id) if self.enabled else None
        self._queue: Queue[tuple[str, str, str, str, str]] = Queue(maxsize=2000)
        self._lock = Lock()
        self._started = False
        self._last_error = ""

    def start(self) -> None:
        if not self.enabled or self._started:
            return
        self._started = True
        Thread(target=self._run, name="GoogleSheetActivityLog", daemon=True).start()

    def record(self, event: str, details: str = "") -> None:
        if not self.enabled:
            return
        row = (
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            socket.gethostname()[:100],
            getuser()[:100],
            str(event)[:120],
            str(details)[:300],
        )
        try:
            self._queue.put_nowait(row)
        except Full:
            self._last_error = "Activity queue is full; some log entries were skipped."

    def _ensure_tab(self) -> None:
        assert self._sheets is not None
        titles = self._sheets.tab_titles()
        if LOG_TAB not in titles:
            try:
                self._sheets._call(
                    ":batchUpdate",
                    {"requests": [{"addSheet": {"properties": {"title": LOG_TAB}}}]},
                )
            except Exception:
                # Two installations can start at the same time. If the other
                # one created the tab first, continue after verifying it exists.
                titles = self._sheets.tab_titles()
                if LOG_TAB not in titles:
                    raise
        if not self._sheets.read(LOG_TAB):
            self._sheets._call(
                "/values:batchUpdate",
                {
                    "valueInputOption": "RAW",
                    "data": [{"range": f"'{LOG_TAB}'!A1:E1", "values": [list(HEADERS)]}],
                },
            )

    def _append(self, rows: list[tuple[str, str, str, str, str]]) -> None:
        assert self._sheets is not None
        encoded = urllib.parse.quote(f"'{LOG_TAB}'!A:E", safe="")
        self._sheets._call(
            f"/values/{encoded}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS",
            {"values": [list(row) for row in rows]},
        )

    def _run(self) -> None:
        while True:
            try:
                first = self._queue.get(timeout=1)
            except Empty:
                continue
            batch = [first]
            while len(batch) < 25:
                try:
                    batch.append(self._queue.get_nowait())
                except Empty:
                    break
            try:
                with self._lock:
                    self._ensure_tab()
                    self._append(batch)
                self._last_error = ""
            except Exception as exc:
                # Cloud logging must never block a QuickBooks action.
                self._last_error = str(exc)[:500]

    def latest(self) -> dict[str, object]:
        if not self.enabled or self._sheets is None:
            return {
                "enabled": False,
                "rows": [],
                "message": "Cloud activity logging is unavailable: the local service-account key is missing.",
            }
        try:
            with self._lock:
                titles = self._sheets.tab_titles()
                if LOG_TAB not in titles:
                    return {"enabled": True, "rows": [], "message": "Waiting for the first log entry."}
                values = self._sheets.read(LOG_TAB)
            rows = [
                dict(zip(HEADERS, row))
                for row in values[1:]
                if any(str(cell).strip() for cell in row)
            ]
            rows = rows[-MAX_ROWS:]
            rows.reverse()
            return {
                "enabled": True,
                "rows": rows,
                "message": self._last_error or f"Refreshing every 5 seconds · showing up to {MAX_ROWS} recent events.",
            }
        except Exception as exc:
            return {
                "enabled": True,
                "rows": [],
                "message": f"Could not read the shared activity sheet: {str(exc)[:400]}",
            }
