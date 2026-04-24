"""Pydantic models for extracted invoices and the export.json state file."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

FakturoidStatusValue = Literal["pending", "imported", "error", "skipped"]


class VendorInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    ico: str | None = None
    dic: str | None = None
    address: str | None = None

    @field_validator("ico", mode="before")
    @classmethod
    def _normalize_ico(cls, v: object) -> str | None:
        if v is None or v == "":
            return None
        if isinstance(v, int):
            v = str(v)
        if not isinstance(v, str):
            raise TypeError("ico must be a string or int")
        digits = v.strip()
        if not digits:
            return None
        if not digits.isdigit():
            raise ValueError(f"ico must be digits only, got {v!r}")
        return digits.zfill(8)


class InvoiceLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    quantity: Decimal = Decimal("1")
    unit_name: str | None = None
    unit_price: Decimal
    vat_rate: int = 21

    @field_validator("vat_rate")
    @classmethod
    def _check_vat_rate(cls, v: int) -> int:
        if v not in (0, 10, 12, 15, 21):
            raise ValueError(f"vat_rate must be one of 0/10/12/15/21, got {v}")
        return v


class FakturoidStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_id: int | None = None
    expense_id: int | None = None
    imported_at: datetime | None = None
    status: FakturoidStatusValue = "pending"
    error: str | None = None


class ExtractedInvoice(BaseModel):
    """The LLM-extracted invoice payload (editable by the user)."""

    model_config = ConfigDict(extra="forbid")

    vendor: VendorInfo
    invoice_number: str
    issued_on: date
    due_date: date | None = None
    taxable_fulfillment_due: date | None = None
    currency: str = "CZK"
    lines: list[InvoiceLine] = Field(default_factory=list)
    total: Decimal | None = None
    total_vat: Decimal | None = None


class ExportRecord(BaseModel):
    """One invoice in the export.json batch — extracted data + Fakturoid state."""

    model_config = ConfigDict(extra="forbid")

    id: str
    source_pdf: str
    extracted_at: datetime
    vendor: VendorInfo
    invoice_number: str
    issued_on: date
    due_date: date | None = None
    taxable_fulfillment_due: date | None = None
    currency: str = "CZK"
    lines: list[InvoiceLine] = Field(default_factory=list)
    total: Decimal | None = None
    total_vat: Decimal | None = None
    fakturoid: FakturoidStatus = Field(default_factory=FakturoidStatus)

    @classmethod
    def from_extraction(
        cls,
        *,
        invoice_id: str,
        source_pdf: str,
        extracted_at: datetime,
        extracted: ExtractedInvoice,
    ) -> ExportRecord:
        return cls(
            id=invoice_id,
            source_pdf=source_pdf,
            extracted_at=extracted_at,
            vendor=extracted.vendor,
            invoice_number=extracted.invoice_number,
            issued_on=extracted.issued_on,
            due_date=extracted.due_date,
            taxable_fulfillment_due=extracted.taxable_fulfillment_due,
            currency=extracted.currency,
            lines=list(extracted.lines),
            total=extracted.total,
            total_vat=extracted.total_vat,
        )


class ExportFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    created_at: datetime
    invoices: list[ExportRecord] = Field(default_factory=list)
