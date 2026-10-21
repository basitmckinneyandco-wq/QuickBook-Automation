"""Reading and writing the Google Sheet directly, as a service account.

The alternative - a published Apps Script web app - answers in 36 to 49
seconds and refuses about half of all requests, and it can only be read.
This talks to the Sheets API instead: a couple of seconds, and it can write
back, which is what lets two dashboards see each other's work.

Only `google-auth` is needed; the API itself is plain HTTPS and JSON, so
there is no client library to carry into the packaged .exe.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable, Sequence

API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)

# The four columns a dashboard writes so the other one can see what it did.
# Matched by header text, because their position differs between tabs - the
# New Business and Renewal sheets are one column out from each other.
STATUS_HEADERS = {
    "qb_customer_job": "qb customer/job",
    "qb_premium": "qb premium",
    "qb_commission": "qb commission",
    "qb_fees": "qb fees",
}


class SheetsError(RuntimeError):
    """The sheet could not be read or written."""


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA. The A1 notation the API expects."""
    name = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


class Sheets:
    """One spreadsheet, opened as the service account."""

    def __init__(self, key_file: str, spreadsheet_id: str, timeout: int = 180):
        self.key_file = str(key_file)
        self.spreadsheet_id = spreadsheet_id
        self.timeout = timeout
        self._credentials = None

    # ── signing in ──────────────────────────────────────────────────────

    def _token(self) -> str:
        """A valid access token, refreshed when it has expired."""
        try:
            from google.auth.transport.requests import Request
            from google.oauth2 import service_account
        except ImportError as exc:
            raise SheetsError(
                "The Google libraries are missing. Run: pip install -r requirements.txt"
            ) from exc

        if not Path(self.key_file).is_file():
            raise SheetsError(
                f"The service account key was not found at {self.key_file}. "
                "Save the JSON key file there - it is what lets the app sign in "
                "to the sheet."
            )
        if self._credentials is None:
            try:
                self._credentials = service_account.Credentials.from_service_account_file(
                    self.key_file, scopes=list(SCOPES)
                )
            except Exception as exc:
                raise SheetsError(f"That key file could not be read: {exc}") from exc
        if not self._credentials.valid:
            try:
                self._credentials.refresh(Request())
            except Exception as exc:
                # The key is signed with the time on this computer. A clock a
                # few minutes out is refused as "invalid_grant", which says
                # nothing about clocks and sends people hunting the key file.
                if "invalid_grant" in str(exc):
                    raise SheetsError(
                        "Google refused the key because this computer's clock is "
                        "wrong. Right-click the clock, choose Adjust date and time, "
                        "and turn on 'Set time automatically' - then try again. "
                        f"({exc})"
                    ) from exc
                raise SheetsError(f"Google refused the key: {exc}") from exc
        return str(self._credentials.token)

    @property
    def account_email(self) -> str:
        self._token()
        return str(getattr(self._credentials, "service_account_email", ""))

    # ── talking to the API ──────────────────────────────────────────────

    def _call(self, path: str, body: dict | None = None) -> dict:
        url = f"{API}/{self.spreadsheet_id}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": "application/json",
            },
            method="POST" if body is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise SheetsError(self._explain(exc)) from exc
        except urllib.error.URLError as exc:
            raise SheetsError(f"Could not reach Google Sheets: {exc.reason}") from exc

    def _explain(self, exc: urllib.error.HTTPError) -> str:
        """Say what a refusal means in terms of what to do about it."""
        try:
            detail = json.load(exc).get("error", {}).get("message", "")
        except Exception:
            detail = exc.reason
        if exc.code in (403, 404):
            return (
                f"Google would not open the sheet ({exc.code}). Check the sheet is "
                f"shared with {self.account_email or 'the service account'} as an "
                f"Editor, and that the spreadsheet id is right. ({detail})"
            )
        return f"Google Sheets replied {exc.code}: {detail}"

    # ── reading ─────────────────────────────────────────────────────────

    def tab_titles(self) -> list[str]:
        info = self._call("?fields=sheets.properties.title")
        return [sheet["properties"]["title"] for sheet in info.get("sheets", [])]

    def read(self, title: str) -> list[list[object]]:
        """Every value in one tab, as rows. Row 1 of the tab is rows[0]."""
        rng = urllib.parse.quote(f"'{title}'")
        payload = self._call(f"/values/{rng}?valueRenderOption=FORMATTED_VALUE")
        return payload.get("values", [])

    # ── writing ─────────────────────────────────────────────────────────

    def write(self, updates: Sequence[tuple[str, str]]) -> int:
        """Set individual cells. `updates` is [(A1 range, value), ...].

        Sent as one request whatever its size: a batch of fifty rows is fifty
        times the cost of one otherwise, and Google rate-limits by request.
        """
        if not updates:
            return 0
        body = {
            "valueInputOption": "RAW",
            "data": [
                {"range": rng, "values": [[value]]} for rng, value in updates
            ],
        }
        payload = self._call("/values:batchUpdate", body)
        return int(payload.get("totalUpdatedCells", 0))


def status_columns(rows: Iterable[Sequence[object]], limit: int = 12) -> dict[str, int]:
    """Where the four QB status columns are, found by their header text.

    Their position is not fixed: the two pipelines differ by a column, and a
    column inserted upstream would move them again. Matching on the header is
    the only way that stays true.
    """
    for row in list(rows)[:limit]:
        lowered = [str(cell).strip().casefold() for cell in row]
        found = {
            field: lowered.index(header)
            for field, header in STATUS_HEADERS.items()
            if header in lowered
        }
        if len(found) == len(STATUS_HEADERS):
            return found
    return {}
