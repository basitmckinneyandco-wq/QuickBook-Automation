"""Apply a human's correction to one field of one row.

The Google Sheet is read-only to us, so edits live here instead: they are kept
in the local database and re-applied every time the sheet is downloaded again.
That way fixing a 45-character customer name survives a refresh.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Mapping

from .importer import _date, _money, _text
from .models import WorkRecord

TEXT_FIELDS = frozenset(
    {"customer_name", "job_name", "item", "invoice_number", "description",
     "company_name", "email", "phone"}
)
MONEY_FIELDS = frozenset({"amount", "rate"})
COUNT_FIELDS = frozenset({"quantity"})
DATE_FIELDS = frozenset({"invoice_date"})


class EditError(ValueError):
    """The value given cannot be stored in that field."""


def editable(record: WorkRecord) -> set[str]:
    return set(TEXT_FIELDS | MONEY_FIELDS | COUNT_FIELDS | DATE_FIELDS | dict(record.extra).keys())


def apply(record: WorkRecord, field: str, value: str) -> WorkRecord:
    """Return a copy of `record` with `field` set to `value`."""
    text = _text(value)

    if field in TEXT_FIELDS:
        return replace(record, **{field: text})

    if field in MONEY_FIELDS or field in COUNT_FIELDS:
        if not text:
            amount = Decimal("0")
        else:
            amount = _money(text)
            if amount == 0 and not _looks_numeric(text):
                raise EditError(f"'{value}' is not a number.")
        if field in MONEY_FIELDS:
            amount = amount.quantize(Decimal("0.01"))
        return replace(record, **{field: amount})

    if field in DATE_FIELDS:
        if not text:
            return replace(record, invoice_date=None)
        parsed = _date(text)
        if parsed is None:
            raise EditError(f"'{value}' is not a date we recognise. Try 2026-01-06.")
        return replace(record, invoice_date=parsed)

    extra = dict(record.extra)
    if field not in extra:
        raise EditError(f"'{field}' is not an editable column.")
    extra[field] = text
    return replace(record, extra=tuple(extra.items()))


def apply_all(record: WorkRecord, values: Mapping[str, str]) -> WorkRecord:
    """Re-apply every stored correction for a row, ignoring ones that no longer fit."""
    for field, value in values.items():
        try:
            record = apply(record, field, value)
        except EditError:
            continue
    return record


def _looks_numeric(text: str) -> bool:
    return any(character.isdigit() for character in text)
