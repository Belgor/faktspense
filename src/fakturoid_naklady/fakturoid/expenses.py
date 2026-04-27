"""Build and POST expenses to Fakturoid, with PDF attached as base64 data URI."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from ..models import ExportRecord
from .client import FakturoidClient


def build_expense_payload(
    record: ExportRecord,
    *,
    subject_id: int,
    pdf_bytes: bytes,
    pdf_filename: str,
) -> dict[str, Any]:
    """Pure: build the JSON body to POST to /expenses.json."""
    if not record.id:
        raise ValueError("record.id is required — used as custom_id for idempotency")
    lines = [
        {
            "name": line.name,
            "quantity": str(line.quantity),
            "unit_name": line.unit_name,
            "unit_price": str(line.unit_price),
            "vat_rate": line.vat_rate,
        }
        for line in record.lines
    ]
    b64 = base64.b64encode(pdf_bytes).decode("ascii")
    payload: dict[str, Any] = {
        "custom_id": record.id,
        "subject_id": subject_id,
        "original_number": record.invoice_number,
        "issued_on": record.issued_on.isoformat(),
        "currency": record.currency,
        "lines": lines,
        "attachments": [
            {
                "filename": pdf_filename,
                "data_url": f"data:application/pdf;base64,{b64}",
            }
        ],
    }
    if record.due_date is not None and record.due_date >= record.issued_on:
        payload["due_on"] = record.due_date.isoformat()
    if record.taxable_fulfillment_due is not None:
        payload["taxable_fulfillment_due"] = record.taxable_fulfillment_due.isoformat()
    return payload


def create_expense(
    client: FakturoidClient,
    record: ExportRecord,
    *,
    subject_id: int,
    pdf_path: Path,
) -> dict[str, Any]:
    """POST the expense; returns the parsed response JSON."""
    pdf_bytes = pdf_path.read_bytes()
    payload = build_expense_payload(
        record,
        subject_id=subject_id,
        pdf_bytes=pdf_bytes,
        pdf_filename=pdf_path.name,
    )
    resp = client.request("POST", client.account_url("/expenses.json"), json=payload)
    return resp.json()
