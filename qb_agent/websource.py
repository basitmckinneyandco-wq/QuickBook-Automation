"""Read policy rows straight from the Google Apps Script JSON feed.

The feed hands back {tab_name: [[cell, ...], ...]} - the raw grid, including the
banner rows above the real header.

Two things about that sheet drive the whole of this file:

1. It has TWO blocks of columns. Columns 0-49 are the working CRM columns; the
   block from "policy type" onwards is the *verified* accounting data, and that
   is the block accounting actually invoices from.
2. Several header names appear in both blocks - "Corporation Name", "Carrier",
   "Line of Business". So when a name appears twice we deliberately take the
   RIGHTMOST one, which is always the verified copy.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from time import sleep
from typing import Sequence

from .importer import _date, _money, _text
from .models import WorkRecord, deterministic_source_id

FETCH_TIMEOUT = 120
MAX_ROWS = 5000

# What the sheet's tabs are called in the dashboard.
# What each tab is called on screen. Only the label: a policy's saved identity
# is built from the raw tab name, so renaming a label never loses its history.
TAB_LABELS = {"nb": "New Business", "renewal": "Renewal"}


def tab_label(tab: str) -> str:
    return TAB_LABELS.get(tab.strip().casefold(), tab)

# field -> the header spellings that feed it, matched case-insensitively.
FIELDS: dict[str, tuple[str, ...]] = {
    "customer_name": ("corporation name", "account name"),
    "job_name": ("line of business", "lob"),
    "item": ("carrier",),
    "invoice_number": ("policy no#", "policy no", "policy number"),
    # Renewal keeps the expiring policy in the CRM block and the new policy in
    # the verified block.  It is used only to locate the existing QB customer.
    "old_policy_number": ("expiring policy number",),
    # New Business keeps the insured location in the CRM block (column J).
    # It is used only to locate an existing QuickBooks customer location.
    "location": ("location", "location address", "insured location", "risk location", "physical location", "address"),
    "invoice_date": ("eff", "effective date"),
    "description": ("comm inv", "commission invoice"),
    "amount": ("commision", "commission"),
    "commission_rate": ("commission %", "commission rate"),
    "premium": ("pure premium (verfied)", "pure premium (verified)", "pure prem"),
    "total_premium": ("total premium (verfied)", "total premium (verified)", "total prem"),
    "mckinney_fee": ("mckinney fee",),
    "billing": ("bulling", "billing", "billing type"),
    "policy_type": ("policy type",),
    "sheet_status": ("status",),
    "lead_source": ("lead source",),
    "net_due": ("net due",),
    # The sheet renamed "net inv" to "Prem inv"; both spellings are accepted so
    # an older copy of the sheet still maps.
    "prem_inv": ("prem inv", "premium invoice", "net inv"),
    # The two tabs spell these differently - "Fees Invoce" vs "Fees Invoice",
    # "Class" vs "Classs" - so every spelling is listed.
    "fees_invoice": ("fees invoce", "fees invoice", "fee invoice"),
    "policy_class": ("class", "classs"),
    "carrier_type": ("carrier type",),
    # What a dashboard has already done with the row. Read as well as written:
    # this is how one machine sees another's work. Without these the columns
    # exist in the sheet but arrive empty, and a read would quietly wipe them.
    "qb_customer_job": ("qb customer/job", "qb customer job"),
    "qb_premium": ("qb premium",),
    "qb_commission": ("qb commission",),
    "qb_fees": ("qb fees",),
    "qb_customer_target": ("qb customer target",),
    "qb_customer_id": ("qb customer id",),
    "qb_customer_action": ("qb customer action",),
    "qb_job_target": ("qb job target",),
    "qb_job_id": ("qb job id",),
}

# Columns shown in the dashboard but never sent to QuickBooks.
DISPLAY_FIELDS = (
    "policy_type", "billing", "carrier_type", "lead_source", "sheet_status",
    "bound_status",
    "premium", "total_premium", "mckinney_fee", "commission_rate",
    "net_due", "prem_inv", "fees_invoice", "policy_class",
    # What another dashboard has already done with this row. Empty when the
    # sheet has no status columns, or when nobody has sent it yet.
    "qb_customer_job", "qb_premium", "qb_commission", "qb_fees",
    "qb_customer_target", "qb_customer_id", "qb_customer_action",
    "qb_job_target", "qb_job_id",
    "old_policy_number", "location",
)

# Only rows the agency has finished with belong in QuickBooks: the CRM column
# must say the policy was bound, and the accounting column must say the work is
# done. Anything else is still in progress, cancelled or a data-entry mistake.
BOUND_VALUES = ("bound",)
DONE_VALUES = ("done",)


def _is_accounting_ready(bound: str, status: str) -> bool:
    return bound.strip().casefold() in BOUND_VALUES and status.strip().casefold() in DONE_VALUES


class WebSourceError(RuntimeError):
    """The feed could not be fetched or made sense of."""


# Apps Script refuses this feed intermittently - measured at roughly one
# request in two, alternating 200 and 404 on the very same link within a
# minute. It is load-shedding a large response, not a wrong or expired link,
# so a failure means "ask again". Five attempts turn a coin-flip into about a
# one-in-thirty chance of the whole read failing; the previous rows stay on
# screen either way, so patience costs nothing and a hard failure costs a
# stale dashboard.
RETRY_CODES = {404, 408, 429, 500, 502, 503, 504}
ATTEMPTS = 5


def _http_reason(url: str, exc: "urllib.error.HTTPError") -> str:
    """Say what a failure means, not just what code came back."""
    if exc.code == 404 and "script.google" in url:
        return (
            f"Google refused the sheet {ATTEMPTS} times in a row (404). The link "
            "is usually fine - this feed is refused intermittently when Google is "
            "busy, and the next read normally succeeds. The rows already loaded "
            "are still being shown. If it keeps failing for more than a few "
            "minutes, check the sheet is still shared and published."
        )
    return f"The data source replied {exc.code} {exc.reason}."


def _download(url: str, timeout: int) -> object:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) QB-Automation/1.0",
            "Accept": "application/json, text/plain, */*",
        },
    )
    last: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in RETRY_CODES or attempt == ATTEMPTS:
                raise WebSourceError(_http_reason(url, exc)) from exc
        except urllib.error.URLError as exc:
            last = exc
            if attempt == ATTEMPTS:
                raise WebSourceError(f"Could not reach the data source: {exc.reason}") from exc
        except (json.JSONDecodeError, ValueError) as exc:
            last = exc
            if attempt == ATTEMPTS:
                raise WebSourceError("The data source did not return valid JSON.") from exc
        sleep(1.5 * attempt)
    raise WebSourceError(f"The data source could not be read: {last}")


@dataclass(frozen=True, slots=True)
class WebSourceResult:
    records: list[WorkRecord]
    tab: str
    tabs: list[str]
    total_rows: int
    skipped_rows: int
    warnings: list[str]
    columns: dict[str, str]


def fetch_tabs(url: str, timeout: int = FETCH_TIMEOUT) -> dict[str, list[list[object]]]:
    """Download the feed. Returns {tab name: rows}."""
    if not url.strip():
        raise WebSourceError("No data source URL is configured.")
    payload = _download(url, timeout)

    if isinstance(payload, list):  # a single unnamed grid
        payload = {"sheet": payload}
    if not isinstance(payload, dict) or not payload:
        raise WebSourceError("The data source returned no sheets.")

    tabs = {
        str(name): rows
        for name, rows in payload.items()
        if isinstance(rows, list) and rows and isinstance(rows[0], list)
    }
    if not tabs:
        raise WebSourceError("The data source returned no usable rows.")
    return tabs


def build_records(
    tab: str, rows: Sequence[Sequence[object]], *, every_row: bool = False
) -> WebSourceResult:
    """Read a tab into policies.

    `every_row` keeps the ones that are not bound and done yet. They are no use
    for invoicing, which is why they are dropped by default - but they are
    still rows in the sheet, and finding a row by what identifies it must not
    depend on whether it happens to be ready to work on.
    """
    header_index, header = _find_header(rows)
    columns, warnings = _map_columns(header)
    # New Business's location is a CRM field to the left of the verified
    # accounting block. Prefer its header, but accept the confirmed column-J
    # layout when that one cell has an unhelpful/blank header. Do not apply this
    # positional fallback to the headerless export: its column J is the LOB.
    lowered = [name.strip().casefold() for name in header]
    if tab_label(tab).casefold() == "new business":
        location_headers = set(FIELDS["location"])
        crm_locations = [
            index for index, name in enumerate(lowered)
            if name in location_headers and index < _block_start(lowered)
        ]
        if crm_locations:
            columns["location"] = crm_locations[-1]
        elif header_index >= 0 and _block_start(lowered) > 9 and len(header) > 9:
            columns["location"] = 9
    # A feed that names its columns is matched by name. One that does not is
    # matched by position, but only after its shape has been recognised.
    pre_filtered = False
    if "customer_name" not in columns:
        layout = _fixed_layout(rows)
        if layout is None:
            raise WebSourceError(
                f"Tab '{tab}' has no 'Corporation Name' column and does not match the "
                f"{FIXED_WIDTH}-column export either, so its columns cannot be identified."
            )
        header_index, columns, warnings = -1, layout, []
        # As wide as the layout actually reaches: with the status columns it
        # reads up to column 24, and a 21-long list would be indexed past its end.
        header = [f"column {index}" for index in range(max(layout.values()) + 1)]
        # This export is narrowed to bound + done before it reaches us.
        pre_filtered = True

    if "invoice_number" not in columns:
        warnings.append("No 'Policy No#' column was found, so no invoices can be created.")

    if "bound_status" not in columns and not pre_filtered:
        warnings.append(
            "No CRM 'Status' column was found, so rows cannot be limited to bound policies."
        )

    records: list[WorkRecord] = []
    skipped = 0
    not_ready = 0
    data_rows = rows[header_index + 1 :]
    for offset, row in enumerate(data_rows):
        customer = _text(_cell(row, columns.get("customer_name")))
        if not customer:
            # No verified corporation name means the row is not accounting-ready.
            skipped += 1
            continue
        if not every_row and "bound_status" in columns and not _is_accounting_ready(
            _text(_cell(row, columns.get("bound_status"))),
            _text(_cell(row, columns.get("sheet_status"))),
        ):
            # Still in progress, cancelled, or a mistake - not ours to invoice.
            not_ready += 1
            continue
        records.append(_build(tab, header_index + offset + 2, row, columns))
        if len(records) >= MAX_ROWS:
            warnings.append(f"Only the first {MAX_ROWS} rows were loaded.")
            break

    if not records and not every_row:
        raise WebSourceError(
            f"Tab '{tab}' has {len(data_rows)} rows but none are bound and done."
        )
    if not_ready:
        warnings.append(
            f"{not_ready} rows are not bound and done yet, so they were left out."
        )

    return WebSourceResult(
        records=records,
        tab=tab,
        tabs=[],
        total_rows=len(data_rows),
        skipped_rows=skipped + not_ready,
        warnings=warnings,
        columns={field: header[index] for field, index in columns.items()},
    )


def _build(
    tab: str,
    row_number: int,
    row: Sequence[object],
    columns: dict[str, int],
) -> WorkRecord:
    customer = _text(_cell(row, columns.get("customer_name")))
    invoice_number = _text(_cell(row, columns.get("invoice_number")))
    amount = _money(_cell(row, columns.get("amount"))).quantize(Decimal("0.01"))
    description = _text(_cell(row, columns.get("description")))
    return WorkRecord(
        source_id=deterministic_source_id(f"web:{tab}", row_number, customer, invoice_number),
        row_number=row_number,
        customer_name=customer,
        job_name=_text(_cell(row, columns.get("job_name"))),
        company_name=customer,
        invoice_number=invoice_number,
        invoice_date=_sheet_date(_cell(row, columns.get("invoice_date"))),
        item=_text(_cell(row, columns.get("item"))),
        description=description,
        quantity=Decimal("1"),
        rate=Decimal("0"),   # the commission IS the line amount; no unit rate
        amount=amount,
        source_tab=tab_label(tab),
        extra=tuple(
            (field, _text(_cell(row, columns.get(field)))) for field in DISPLAY_FIELDS
        ),
    )


def _find_header(rows: Sequence[Sequence[object]]) -> tuple[int, list[str]]:
    """The header is the widest row near the top - the banner rows above it are sparse."""
    best_index, best_score = 0, -1
    for index, row in enumerate(rows[:15]):
        score = sum(1 for cell in row if str(cell).strip())
        if score > best_score:
            best_index, best_score = index, score
    if best_score < 3:
        raise WebSourceError("No header row could be identified in that tab.")
    return best_index, [str(cell).strip() for cell in rows[best_index]]


# ── the headerless export ────────────────────────────────────────────────
#
# The feed can also send a purpose-built grid with no header row at all: 21
# columns, already narrowed to bound + done policies. Nothing names the
# columns, so they are matched by position - which is only safe because the
# shape is checked before it is trusted (see _fixed_layout).
FIXED_WIDTH = 21

# The same export with the four QuickBooks status columns appended, which is
# how two people working the same sheet see each other's work: one dashboard
# writes them, the other reads them back here. They are added at the end, so
# every column before them keeps its meaning.
STATUS_WIDTH = 25
STATUS_LAYOUT = {
    "qb_customer_job": 21,
    "qb_premium": 22,
    "qb_commission": 23,
    "qb_fees": 24,
}

FIXED_LAYOUT = {
    "policy_type": 0,
    "billing": 1,
    "carrier_type": 2,
    "lead_source": 3,
    "sheet_status": 4,
    "invoice_date": 5,
    "customer_name": 6,
    # 7 is a second name column (the insured), which QuickBooks does not use
    "item": 8,            # Carrier
    "job_name": 9,        # Line of Business
    "invoice_number": 10, # Policy No#
    "premium": 11,
    "total_premium": 12,
    "mckinney_fee": 13,
    "commission_rate": 14,
    "amount": 15,         # Commission
    "description": 16,    # Comm Inv
    "net_due": 17,
    "prem_inv": 18,
    "fees_invoice": 19,
    "policy_class": 20,
}


def _fixed_layout(rows: Sequence[Sequence[object]]) -> dict[str, int] | None:
    """Recognise the headerless 21-column export, or refuse to guess.

    Reading by position is only defensible while the shape is exactly the one
    that was verified. Any change - a column inserted, a different width - has
    to fail here and loudly, because silently reading the wrong column would
    put the wrong carrier and the wrong amount on real invoices.
    """
    sample = [row for row in rows if any(str(cell).strip() for cell in row)][:40]
    if not sample:
        return None
    widths = {len(row) for row in sample}
    # Two widths are recognised, and only two: the original export, and that
    # same export with the four status columns appended. Anything else is a
    # change nobody has verified, and is still refused.
    if widths not in ({FIXED_WIDTH}, {STATUS_WIDTH}):
        return None
    # The corporation name and the effective date are what every row must have.
    named = sum(1 for row in sample if str(row[FIXED_LAYOUT["customer_name"]]).strip())
    dated = sum(1 for row in sample if _sheet_date(row[FIXED_LAYOUT["invoice_date"]]))
    if named < len(sample) * 0.8 or dated < len(sample) * 0.8:
        return None
    layout = dict(FIXED_LAYOUT)
    if widths == {STATUS_WIDTH}:
        layout.update(STATUS_LAYOUT)
    return layout


# The verified accounting block starts at one of these headers. Everything left
# of it is the working CRM copy of the same data and must never be invoiced from.
BLOCK_MARKERS = ("policy type", "eff")


def _block_start(lowered: Sequence[str]) -> int:
    """Where the verified accounting block begins, or 0 if there is only one block."""
    starts = [
        index
        for index, name in enumerate(lowered)
        if name in BLOCK_MARKERS
    ]
    return min(starts) if starts else 0


def _map_columns(header: Sequence[str]) -> tuple[dict[str, int], list[str]]:
    """field -> column index, searching only the verified block, rightmost match wins."""
    lowered = [name.strip().casefold() for name in header]
    start = _block_start(lowered)
    columns: dict[str, int] = {}
    # The CRM "Status" (Bound / Not Bound) lives to the LEFT of the verified
    # block, so it is the one header we deliberately look for outside it.
    bound = [index for index, name in enumerate(lowered) if name == "status" and index < start]
    if bound:
        columns["bound_status"] = bound[0]
    # Renewal column R is deliberately outside the verified accounting block.
    # Keep it separate from invoice_number: the latter remains the new policy
    # number from the right-hand verified block (currently column BJ).
    old_policies = [
        index for index, name in enumerate(lowered)
        if name in ("policy number", "expiring policy number") and index < start
    ]
    if old_policies:
        columns["old_policy_number"] = old_policies[-1]
    for field, aliases in FIELDS.items():
        for alias in aliases:
            matches = [
                index
                for index, name in enumerate(lowered)
                if name == alias and index >= start
            ]
            if matches:
                columns[field] = matches[-1]
                break

    notes: list[str] = []
    # The 'renewal' tab leaves the carrier header blank ("Column 56"). In the
    # verified block the carrier always sits immediately left of Line of
    # Business, so fall back to that - and say so, rather than silently guessing.
    if "item" not in columns and "job_name" in columns and columns["job_name"] > 0:
        candidate = columns["job_name"] - 1
        columns["item"] = candidate
        label = header[candidate] if candidate < len(header) else f"column {candidate}"
        notes.append(
            f"No 'Carrier' header was found, so column {candidate} ({label or 'unnamed'}) "
            "is being used as the invoice item. Name that header 'Carrier' in the sheet to be sure."
        )
    return columns, notes


def _cell(row: Sequence[object], index: int | None) -> object:
    if index is None or index >= len(row):
        return ""
    return row[index]


def _sheet_date(value: object) -> date | None:
    """Google serialises sheet dates as UTC timestamps, shifted by the sheet's timezone.

    A cell showing 6 Jan arrives as 2026-01-05T19:00:00Z from a UTC+5 sheet, or as
    2026-01-06T05:00:00Z from a US one. Rounding to the nearest midnight recovers
    the date the human typed in either case.
    """
    text = _text(value)
    if text.endswith("Z") and "T" in text:
        try:
            stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return _date(value)
        return (stamp.astimezone(timezone.utc) + timedelta(hours=12)).date()
    return _date(value)
