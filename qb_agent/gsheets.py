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

# Saved by the customer resolver.  They are hidden in Google Sheets: staff use
# the dashboard picker, while these stable IDs let every computer make the same
# choice next time without asking again.
MAPPING_HEADERS = {
    "qb_customer_target": "qb customer target",
    "qb_customer_id": "qb customer id",
    "qb_customer_action": "qb customer action",
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

    def hide_columns(self, title: str, columns: Sequence[int]) -> None:
        """Hide zero-based columns in one tab; values remain available to the app."""
        wanted = sorted(set(int(column) for column in columns if column >= 0))
        if not wanted:
            return
        info = self._call("?fields=sheets.properties(sheetId,title)")
        sheet_id = next(
            (
                sheet.get("properties", {}).get("sheetId")
                for sheet in info.get("sheets", [])
                if sheet.get("properties", {}).get("title") == title
            ),
            None,
        )
        if sheet_id is None:
            raise SheetsError(f"The sheet tab '{title}' could not be found.")
        self._call(
            ":batchUpdate",
            {
                "requests": [
                    {
                        "updateDimensionProperties": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "COLUMNS",
                                "startIndex": column,
                                "endIndex": column + 1,
                            },
                            "properties": {"hiddenByUser": True},
                            "fields": "hiddenByUser",
                        }
                    }
                    for column in wanted
                ]
            },
        )

    def ensure_column_count(self, title: str, required: int) -> None:
        """Expand one tab when a new value would fall beyond its grid limit.

        The Values API does not automatically grow a sheet.  It instead
        returns HTTP 400 "range exceeds grid limits", so dashboard-owned
        columns must be added to the grid before their headers are written.
        """
        info = self._call(
            "?fields=sheets.properties(sheetId,title,gridProperties.columnCount)"
        )
        properties = next(
            (
                sheet.get("properties", {})
                for sheet in info.get("sheets", [])
                if sheet.get("properties", {}).get("title") == title
            ),
            None,
        )
        if properties is None:
            raise SheetsError(f"The sheet tab '{title}' could not be found.")
        current = int(properties.get("gridProperties", {}).get("columnCount", 0))
        if current >= required:
            return
        self._call(
            ":batchUpdate",
            {
                "requests": [
                    {
                        "appendDimension": {
                            "sheetId": properties.get("sheetId"),
                            "dimension": "COLUMNS",
                            "length": required - current,
                        }
                    }
                ]
            },
        )


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


def sheet_columns(rows: Iterable[Sequence[object]], limit: int = 12) -> dict[str, int]:
    """All dashboard-owned columns already present in a tab."""
    headers = {**STATUS_HEADERS, **MAPPING_HEADERS}
    best: dict[str, int] = {}
    for row in list(rows)[:limit]:
        lowered = [str(cell).strip().casefold() for cell in row]
        found = {
            field: lowered.index(header)
            for field, header in headers.items()
            if header in lowered
        }
        if len(found) > len(best):
            best = found
    return best


def dashboard_header_row(rows: Iterable[Sequence[object]], limit: int = 12) -> int:
    """One-based row containing the existing QB status headers."""
    expected = set(STATUS_HEADERS.values())
    for number, row in enumerate(list(rows)[:limit], start=1):
        lowered = {str(cell).strip().casefold() for cell in row}
        if lowered & expected:
            return number
    return 1
