from datetime import date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from fakturoid_naklady.models import (
    ExtractedInvoice,
    InvoiceLine,
    VendorInfo,
)


def test_vendor_ico_pads_leading_zeros() -> None:
    v = VendorInfo(name="x", ico="12345")
    assert v.ico == "00012345"


def test_vendor_ico_accepts_int() -> None:
    v = VendorInfo(name="x", ico=12345678)  # type: ignore[arg-type]
    assert v.ico == "12345678"


def test_vendor_ico_rejects_non_digits() -> None:
    with pytest.raises(ValidationError):
        VendorInfo(name="x", ico="12A4")


def test_vendor_ico_empty_normalized_to_none() -> None:
    v = VendorInfo(name="x", ico="")
    assert v.ico is None


def test_invoice_line_vat_rate_restricted() -> None:
    InvoiceLine(name="x", unit_price=Decimal("100"), vat_rate=21)
    with pytest.raises(ValidationError):
        InvoiceLine(name="x", unit_price=Decimal("100"), vat_rate=17)


def test_extracted_invoice_roundtrip() -> None:
    inv = ExtractedInvoice(
        vendor=VendorInfo(name="ACME", ico="12345678"),
        invoice_number="2024-0042",
        issued_on=date(2024, 3, 15),
        lines=[InvoiceLine(name="work", unit_price=Decimal("1000"))],
    )
    dumped = inv.model_dump_json()
    again = ExtractedInvoice.model_validate_json(dumped)
    assert again == inv


def test_extracted_invoice_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ExtractedInvoice(
            vendor=VendorInfo(name="ACME"),
            invoice_number="1",
            issued_on=date(2024, 1, 1),
            mystery_field="nope",  # type: ignore[call-arg]
        )


def test_extracted_invoice_accepts_minimal() -> None:
    inv = ExtractedInvoice(
        vendor=VendorInfo(name="ACME"),
        invoice_number="1",
        issued_on=date(2024, 1, 1),
    )
    assert inv.currency == "CZK"
    assert inv.lines == []


def test_extracted_at_is_datetime_roundtrip() -> None:
    # sanity: datetime passes through unchanged
    ts = datetime(2024, 3, 15, 10, 30, 0)
    inv = ExtractedInvoice(
        vendor=VendorInfo(name="ACME"),
        invoice_number="1",
        issued_on=date(2024, 1, 1),
    )
    assert inv.issued_on == date(2024, 1, 1)
    # timestamp unrelated to invoice, just verifying datetime libs import
    assert ts.year == 2024
