from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from fakturoid_naklady.fakturoid.auth import StaticTokenProvider
from fakturoid_naklady.fakturoid.client import FakturoidClient
from fakturoid_naklady.fakturoid.subjects import SubjectStore
from fakturoid_naklady.models import (
    ExportRecord,
    ExtractedInvoice,
    FakturoidStatus,
    InvoiceLine,
    VendorInfo,
)
from fakturoid_naklady.pipeline import (
    AlreadyImportedError,
    ImportFlags,
    ImportRunner,
    VendorNotFoundError,
)


def _record(tmp_path: Path, ico: str = "12345678") -> ExportRecord:
    pdf = tmp_path / "acme.pdf"
    pdf.write_bytes(b"%PDF-fake")
    extracted = ExtractedInvoice(
        vendor=VendorInfo(name="ACME", ico=ico),
        invoice_number="2024-0042",
        issued_on=date(2024, 3, 15),
        lines=[InvoiceLine(name="w", unit_price=Decimal("100"))],
    )
    return ExportRecord.from_extraction(
        invoice_id="a" * 64,
        source_pdf=str(pdf),
        extracted_at=datetime(2024, 3, 15, tzinfo=UTC),
        extracted=extracted,
    )


def _build_runner(
    tmp_path: Path, *, vendor_prompt: Any = None
) -> tuple[ImportRunner, FakturoidClient, httpx.Client]:
    http = httpx.Client()
    client = FakturoidClient(slug="acme", http=http, token_provider=StaticTokenProvider("tkn"))
    subjects = SubjectStore(client=client, cache_path=tmp_path / "subjects.json")
    runner = ImportRunner(
        client=client,
        subjects=subjects,
        pdf_root=tmp_path,
        vendor_prompt=vendor_prompt or (lambda v, c: ("create", None)),
        now=lambda: datetime(2024, 4, 1, tzinfo=UTC),
    )
    return runner, client, http


def test_already_imported_raises(tmp_path: Path) -> None:
    rec = _record(tmp_path)
    rec.fakturoid = FakturoidStatus(status="imported", expense_id=99)
    runner, _, http = _build_runner(tmp_path)
    try:
        with pytest.raises(AlreadyImportedError):
            runner.run_one(rec, ImportFlags())
    finally:
        http.close()


def test_no_create_flag_raises_when_vendor_missing(tmp_path: Path, httpx_mock: HTTPXMock) -> None:
    rec = _record(tmp_path)
    # empty cache → fetch returns no matching subject
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/subjects.json?page=1",
        json=[],
    )
    runner, _, http = _build_runner(tmp_path)
    try:
        with pytest.raises(VendorNotFoundError):
            runner.run_one(rec, ImportFlags(no_create=True))
    finally:
        http.close()


def test_auto_create_creates_subject_and_posts_expense(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    rec = _record(tmp_path)
    # subject lookup: cache miss, fetch returns empty
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/subjects.json?page=1",
        json=[],
    )
    # subject creation
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/subjects.json",
        method="POST",
        json={"id": 7, "name": "ACME", "registration_no": "12345678"},
    )
    # expense creation
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/expenses.json",
        method="POST",
        json={"id": 999},
    )
    runner, _, http = _build_runner(tmp_path)
    try:
        outcome = runner.run_one(rec, ImportFlags(auto_create_subjects=True))
        assert outcome.status == "imported"
        assert outcome.subject_id == 7
        assert outcome.expense_id == 999
        assert outcome.imported_at is not None
    finally:
        http.close()


def test_dry_run_does_not_post_expense(tmp_path: Path, httpx_mock: HTTPXMock) -> None:
    rec = _record(tmp_path)
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/subjects.json?page=1",
        json=[{"id": 7, "name": "ACME", "registration_no": "12345678"}],
    )
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/subjects.json?page=2",
        json=[],
    )
    runner, _, http = _build_runner(tmp_path)
    try:
        outcome = runner.run_one(rec, ImportFlags(dry_run=True))
        assert outcome.status == "pending"
        assert outcome.subject_id == 7
        assert outcome.expense_id is None
    finally:
        http.close()

    # No POST requests were made
    assert all(r.method != "POST" for r in httpx_mock.get_requests())


def test_skip_action_returns_skipped(tmp_path: Path, httpx_mock: HTTPXMock) -> None:
    rec = _record(tmp_path)
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/subjects.json?page=1",
        json=[],
    )
    runner, _, http = _build_runner(tmp_path, vendor_prompt=lambda v, c: ("skip", None))
    try:
        outcome = runner.run_one(rec, ImportFlags())
        assert outcome.status == "skipped"
        assert outcome.expense_id is None
    finally:
        http.close()


def test_map_action_uses_existing_subject(tmp_path: Path, httpx_mock: HTTPXMock) -> None:
    # vendor without an IČO forces the prompt path
    rec = _record(tmp_path)
    rec.vendor = VendorInfo(name="ACME", ico=None)

    existing = {"id": 50, "name": "ACME Czech", "registration_no": "55555555"}
    # empty cache + fetch-all returns the existing candidate (for fuzzy match listing)
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/subjects.json?page=1",
        json=[existing],
    )
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/subjects.json?page=2",
        json=[],
    )
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/expenses.json",
        method="POST",
        json={"id": 1234},
    )

    runner, _, http = _build_runner(tmp_path, vendor_prompt=lambda v, c: ("map", existing))
    try:
        outcome = runner.run_one(rec, ImportFlags())
        assert outcome.status == "imported"
        assert outcome.subject_id == 50
        assert outcome.expense_id == 1234
    finally:
        http.close()
