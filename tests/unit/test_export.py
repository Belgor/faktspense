from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from fakturoid_naklady import export as export_mod
from fakturoid_naklady.models import (
    ExportFile,
    ExportRecord,
    ExtractedInvoice,
    FakturoidStatus,
    FakturoidStatusValue,
    InvoiceLine,
    VendorInfo,
)


def _make_record(rid: str = "abc", status: FakturoidStatusValue = "pending") -> ExportRecord:
    extracted = ExtractedInvoice(
        vendor=VendorInfo(name="ACME", ico="12345678"),
        invoice_number="2024-0001",
        issued_on=date(2024, 1, 1),
        lines=[InvoiceLine(name="x", unit_price=Decimal("100"))],
    )
    rec = ExportRecord.from_extraction(
        invoice_id=rid,
        source_pdf="x.pdf",
        extracted_at=datetime(2024, 1, 1, tzinfo=UTC),
        extracted=extracted,
    )
    rec.fakturoid = FakturoidStatus(status=status)
    return rec


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    ef = export_mod.load(tmp_path / "nope.json")
    assert ef.invoices == []


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "export.json"
    original = ExportFile(created_at=datetime(2024, 1, 1, tzinfo=UTC))
    original.invoices.append(_make_record())
    export_mod.save(path, original)
    loaded = export_mod.load(path)
    assert loaded.invoices[0].id == "abc"
    assert loaded.invoices[0].vendor.ico == "12345678"


def test_save_is_atomic_no_tmp_leftover(tmp_path: Path) -> None:
    path = tmp_path / "export.json"
    ef = ExportFile(created_at=datetime(2024, 1, 1, tzinfo=UTC))
    export_mod.save(path, ef)
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".export.json.")]
    assert leftovers == []


def test_upsert_inserts_new_record() -> None:
    ef = ExportFile(created_at=datetime(2024, 1, 1, tzinfo=UTC))
    export_mod.upsert(ef, _make_record("a"))
    export_mod.upsert(ef, _make_record("b"))
    assert [r.id for r in ef.invoices] == ["a", "b"]


def test_upsert_preserves_existing_fakturoid_state() -> None:
    ef = ExportFile(created_at=datetime(2024, 1, 1, tzinfo=UTC))
    existing = _make_record("a", status="imported")
    existing.fakturoid = FakturoidStatus(
        subject_id=7,
        expense_id=99,
        imported_at=datetime(2024, 1, 5, tzinfo=UTC),
        status="imported",
    )
    ef.invoices.append(existing)

    replacement = _make_record("a", status="pending")
    export_mod.upsert(ef, replacement)

    assert len(ef.invoices) == 1
    merged = ef.invoices[0]
    assert merged.fakturoid.status == "imported"
    assert merged.fakturoid.expense_id == 99


def test_find_by_id() -> None:
    ef = ExportFile(created_at=datetime(2024, 1, 1, tzinfo=UTC))
    ef.invoices.append(_make_record("a"))
    assert export_mod.find_by_id(ef, "a") is not None
    assert export_mod.find_by_id(ef, "missing") is None


def test_update_status_in_place() -> None:
    ef = ExportFile(created_at=datetime(2024, 1, 1, tzinfo=UTC))
    ef.invoices.append(_make_record("a"))
    export_mod.update_status(
        ef,
        "a",
        status="imported",
        subject_id=7,
        expense_id=99,
        imported_at=datetime(2024, 2, 1, tzinfo=UTC),
    )
    rec = export_mod.find_by_id(ef, "a")
    assert rec is not None
    assert rec.fakturoid.status == "imported"
    assert rec.fakturoid.expense_id == 99
    assert rec.fakturoid.subject_id == 7


def test_update_status_missing_raises() -> None:
    ef = ExportFile(created_at=datetime(2024, 1, 1, tzinfo=UTC))
    with pytest.raises(KeyError):
        export_mod.update_status(ef, "nope", status="error")
