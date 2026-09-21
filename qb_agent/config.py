"""Settings, loaded from config.json, plus the rules that decide if writes are allowed."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VALID_MODES = {"dry_run", "quickbooks"}


@dataclass(frozen=True, slots=True)
class CompanyConfig:
    display_name: str
    file_path: str


@dataclass(frozen=True, slots=True)
class DataSourceConfig:
    url: str
    default_tab: str

    @property
    def enabled(self) -> bool:
        return bool(self.url.strip())


@dataclass(frozen=True, slots=True)
class InvoiceKindConfig:
    """Where one kind of policy invoice lands in QuickBooks.

    "Transaction Type" and "Source" are not settings: the templates rename the
    built-in Terms and Rep fields to those labels, so the app writes TermsRef
    and SalesRepRef and there is nothing to configure.
    """

    template: str
    ar_account: str
    # Set when every invoice of this kind uses the same item, as fees do.
    # Left empty when the item comes from the row, as the carrier does.
    item: str = ""
    # Items are created on demand, and a new item needs an income account.
    item_income_account: str = ""


@dataclass(frozen=True, slots=True)
class AppConfig:
    mode: str
    allow_writes: bool
    listen_host: str
    listen_port: int
    database_path: str
    app_name: str
    qbxml_version: str
    invoice_template: str
    premium: InvoiceKindConfig
    commission: InvoiceKindConfig
    fees: InvoiceKindConfig
    company: CompanyConfig
    data_source: DataSourceConfig
    # Where this came from, so the app can write a change back to the same file.
    source_path: str = ""

    def invoice_kind(self, kind: str) -> InvoiceKindConfig:
        return {"commission": self.commission, "fees": self.fees}.get(kind, self.premium)

    @property
    def live_writes_enabled(self) -> bool:
        """Every switch that must be on before a single record reaches QuickBooks."""
        return (
            self.mode == "quickbooks"
            and self.allow_writes
            and bool(self.company.file_path.strip())
        )

    def blocked_reason(self) -> str | None:
        """Why live writes are off - or None when they are on."""
        if self.mode != "quickbooks":
            return "mode is 'dry_run', so nothing is written to QuickBooks."
        if not self.allow_writes:
            return "allow_writes is false in config.json."
        if not self.company.file_path.strip():
            return "company.file_path is empty in config.json."
        return None


def _invoice_kind(raw: Any) -> InvoiceKindConfig:
    settings = raw if isinstance(raw, dict) else {}
    return InvoiceKindConfig(
        template=str(settings.get("template", "")),
        ar_account=str(settings.get("ar_account", "")),
        item=str(settings.get("item", "")),
        item_income_account=str(settings.get("item_income_account", "")),
    )


def load_config(filename: str | Path) -> AppConfig:
    path = Path(filename)
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} was not found. Copy config.example.json to config.json first."
        )
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    mode = str(raw.get("mode", "dry_run"))
    if mode not in VALID_MODES:
        raise ValueError("mode must be 'dry_run' or 'quickbooks'")
    company = raw.get("company", {})
    invoice = raw.get("invoice", {})
    database_path = str(raw.get("database_path", "qb_automation.db"))
    if not Path(database_path).is_absolute():
        database_path = str(path.resolve().parent / database_path)
    return AppConfig(
        mode=mode,
        allow_writes=bool(raw.get("allow_writes", False)),
        listen_host=str(raw.get("listen_host", "127.0.0.1")),
        listen_port=int(raw.get("listen_port", 8770)),
        database_path=database_path,
        app_name=str(raw.get("app_name", "QB Automation Dashboard")),
        qbxml_version=str(raw.get("qbxml_version", "13.0")),
        invoice_template=str(invoice.get("template", "")),
        premium=_invoice_kind(invoice.get("premium", {})),
        commission=_invoice_kind(invoice.get("commission", {})),
        fees=_invoice_kind(invoice.get("fees", {})),
        company=CompanyConfig(
            display_name=str(company.get("display_name", "My Company")),
            file_path=str(company.get("file_path", "")),
        ),
        data_source=DataSourceConfig(
            url=str(raw.get("data_source", {}).get("url", "")),
            default_tab=str(raw.get("data_source", {}).get("default_tab", "")),
        ),
        source_path=str(path.resolve()),
    )


def save_company(config: AppConfig, company_file: str, display_name: str, database: str) -> None:
    """Point the settings file at a different QuickBooks company.

    Only the three values that identify a company are touched; templates,
    accounts and the sheet link are left exactly as they were.
    """
    path = Path(config.source_path)
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    raw.setdefault("company", {})
    raw["company"]["file_path"] = company_file
    raw["company"]["display_name"] = display_name
    # Written as a bare file name whenever it sits beside the settings file, so
    # copying the folder to another machine does not leave it pointing at a
    # drive that only existed on this one. load_config resolves it back.
    beside = Path(path).resolve().parent
    kept = Path(database)
    raw["database_path"] = kept.name if kept.parent == beside else str(kept)
    path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def save_data_source(config: AppConfig, url: str) -> None:
    """Point the settings file at a different published sheet.

    Only the link changes. Which tab is the default, and everything about
    QuickBooks, is left alone.
    """
    path = Path(config.source_path)
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    raw.setdefault("data_source", {})
    raw["data_source"]["url"] = url
    path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def database_for(company_file: str, beside: str) -> str:
    """A separate history file per company.

    The database remembers what has already been created. Pointed at a second
    company it would report everything as done and quietly skip the work, so
    each company gets its own - named after the company file, kept next to the
    settings file.
    """
    stem = Path(company_file).stem.strip() or "company"
    safe = "".join(ch if (ch.isalnum() or ch in " -_") else "_" for ch in stem).strip()
    return str(Path(beside).resolve().parent / f"{safe or 'company'}.db")
