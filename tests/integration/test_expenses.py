from __future__ import annotations

import base64
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from fakturoid_naklady.fakturoid.auth import StaticTokenProvider
from fakturoid_naklady.fakturoid.client import FakturoidClient
from fakturoid_naklady.fakturoid.expenses import build_expense_payload, create_expense
from fakturoid_naklady.models import (
    ExportRecord,
    ExtractedInvoice,
    InvoiceLine,
    VendorInfo,
)


def _make_record() -> ExportRecord:
    extracted = ExtractedInvoice(
        vendor=VendorInfo(name="ACME", ico="12345678"),
        invoice_number="2024-0042",
        issued_on=date(2024, 3, 15),
        due_date=date(2024, 3, 29),
        taxable_fulfillment_due=date(2024, 3, 15),
        lines=[
            InvoiceLine(
                name="Work",
                quantity=Decimal("10"),
                unit_name="h",
                unit_price=Decimal("1000"),
                vat_rate=21,
            )
        ],
    )
    return ExportRecord.from_extraction(
        invoice_id="abcdef" * 10 + "abcd",
        source_pdf="acme.pdf",
        extracted_at=datetime(2024, 3, 15, tzinfo=UTC),
        extracted=extracted,
    )


def test_build_expense_payload_matches_golden() -> None:
    rec = _make_record()
    payload = build_expense_payload(
        rec, subject_id=7, pdf_bytes=b"PDFBYTES", pdf_filename="acme.pdf"
    )
    assert payload["custom_id"] == rec.id
    assert payload["subject_id"] == 7
    assert payload["number"] == "2024-0042"
    assert payload["issued_on"] == "2024-03-15"
    assert payload["due_on"] == "2024-03-29"
    assert payload["taxable_fulfillment_due"] == "2024-03-15"
    assert payload["currency"] == "CZK"
    assert payload["lines"][0]["name"] == "Work"
    assert payload["lines"][0]["unit_price"] == "1000"
    assert payload["lines"][0]["vat_rate"] == 21

    att = payload["attachments"][0]
    assert att["filename"] == "acme.pdf"
    expected = "data:application/pdf;base64," + base64.b64encode(b"PDFBYTES").decode()
    assert att["download_url"] == expected


def test_build_expense_payload_requires_id() -> None:
    rec = _make_record()
    rec.id = ""
    with pytest.raises(ValueError, match="custom_id"):
        build_expense_payload(rec, subject_id=1, pdf_bytes=b"x", pdf_filename="x.pdf")


def test_create_expense_posts_and_returns_response(tmp_path: Path, httpx_mock: HTTPXMock) -> None:
    pdf = tmp_path / "acme.pdf"
    pdf.write_bytes(b"%PDF-fake")

    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/expenses.json",
        method="POST",
        json={"id": 999, "number": "2024-0042"},
    )
    with httpx.Client() as http:
        client = FakturoidClient(slug="acme", http=http, token_provider=StaticTokenProvider("t"))
        resp = create_expense(client, _make_record(), subject_id=7, pdf_path=pdf)
    assert resp["id"] == 999

    req = httpx_mock.get_request(method="POST")
    assert req is not None
    body = json.loads(req.content)
    assert body["subject_id"] == 7
    assert body["attachments"][0]["filename"] == "acme.pdf"
    assert body["attachments"][0]["download_url"].startswith("data:application/pdf;base64,")
