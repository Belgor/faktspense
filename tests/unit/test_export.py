from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from fakturoid_naklady.export import ExportStore, sidecar_filename
from fakturoid_naklady.models import (
    ExportRecord,
    ExtractedInvoice,
    FakturoidStatus,
    FakturoidStatusValue,
    InvoiceLine,
    VendorInfo,
)


def _make_record(
    *,
    rid: str = "a" * 64,
    source_pdf: str = "/tmp/invoice-x.pdf",
    status: FakturoidStatusValue = "pending",
) -> ExportRecord:
    extracted = ExtractedInvoice(
        vendor=VendorInfo(name="ACME", ico="12345678"),
        invoice_number="2024-0001",
        issued_on=date(2024, 1, 1),
        lines=[InvoiceLine(name="x", unit_price=Decimal("100"))],
    )
    rec = ExportRecord.from_extraction(
        invoice_id=rid,
        source_pdf=source_pdf,
        extracted_at=datetime(2024, 1, 1, tzinfo=UTC),
        extracted=extracted,
    )
    rec.fakturoid = FakturoidStatus(status=status)
    return rec


# ---------- filename rule ----------


def test_sidecar_filename_uses_pdf_stem_and_short_hash() -> None:
    rec = _make_record(rid="0123456789abcdef" * 4, source_pdf="/path/to/My Invoice.pdf")
    # space gets sanitized to underscore; first 8 chars of id appended
    assert sidecar_filename(rec) == "My_Invoice_01234567.json"


def test_sidecar_filename_handles_unsafe_chars() -> None:
    rec = _make_record(rid="deadbeef" + "0" * 56, source_pdf="/x/Faktura#2024/01.pdf")
    fname = sidecar_filename(rec)
    assert fname.endswith("_deadbeef.json")
    # No path separators or fragile chars in the leading stem.
    assert "/" not in fname and "#" not in fname


# ---------- read on empty / missing ----------


def test_records_returns_empty_when_dir_missing(tmp_path: Path) -> None:
    store = ExportStore(tmp_path / "does-not-exist")
    assert store.records() == []
    assert store.find_by_id("anything") is None


# ---------- write + roundtrip ----------


def test_upsert_writes_sidecar_with_expected_name(tmp_path: Path) -> None:
    store = ExportStore(tmp_path / "out")
    rec = _make_record(rid="b" * 64, source_pdf="/tmp/foo.pdf")
    store.upsert(rec)

    expected = tmp_path / "out" / f"foo_{'b' * 8}.json"
    assert expected.exists()
    loaded = store.find_by_id(rec.id)
    assert loaded is not None
    assert loaded.id == rec.id


def test_upsert_is_atomic_no_tmp_leftover(tmp_path: Path) -> None:
    store = ExportStore(tmp_path)
    store.upsert(_make_record())
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_upsert_preserves_existing_fakturoid_state(tmp_path: Path) -> None:
    store = ExportStore(tmp_path)
    first = _make_record()
    first.fakturoid = FakturoidStatus(
        subject_id=7,
        expense_id=99,
        imported_at=datetime(2024, 1, 5, tzinfo=UTC),
        status="imported",
    )
    store.upsert(first)

    # Simulate a re-extract that would otherwise reset fakturoid state.
    fresh = _make_record()  # same id and source_pdf, default fakturoid block
    store.upsert(fresh)

    merged = store.find_by_id(first.id)
    assert merged is not None
    assert merged.fakturoid.status == "imported"
    assert merged.fakturoid.expense_id == 99
    assert merged.fakturoid.subject_id == 7


def test_upsert_removes_stale_sidecar_when_pdf_content_changes(tmp_path: Path) -> None:
    store = ExportStore(tmp_path)
    old = _make_record(rid="1" * 64, source_pdf="/tmp/foo.pdf")
    store.upsert(old)
    old_path = store.path_for(old)
    assert old_path.exists()

    # Same source_pdf, different content → different id.
    new = _make_record(rid="2" * 64, source_pdf="/tmp/foo.pdf")
    store.upsert(new)

    assert not old_path.exists()
    assert store.path_for(new).exists()
    # And the directory has exactly one sidecar.
    assert len(list(tmp_path.glob("*.json"))) == 1


# ---------- find / records ----------


def test_records_sorted_deterministically(tmp_path: Path) -> None:
    store = ExportStore(tmp_path)
    store.upsert(_make_record(rid="1" * 64, source_pdf="/tmp/b.pdf"))
    store.upsert(_make_record(rid="2" * 64, source_pdf="/tmp/a.pdf"))
    ids = [r.source_pdf for r in store.records()]
    assert ids == ["/tmp/a.pdf", "/tmp/b.pdf"]


def test_find_by_id_returns_none_for_missing(tmp_path: Path) -> None:
    store = ExportStore(tmp_path)
    store.upsert(_make_record())
    assert store.find_by_id("not-a-real-id") is None


# ---------- update_status ----------


def test_update_status_round_trip(tmp_path: Path) -> None:
    store = ExportStore(tmp_path)
    rec = _make_record(rid="c" * 64)
    store.upsert(rec)

    store.update_status(
        rec.id,
        status="imported",
        subject_id=7,
        expense_id=99,
        imported_at=datetime(2024, 2, 1, tzinfo=UTC),
    )
    again = store.find_by_id(rec.id)
    assert again is not None
    assert again.fakturoid.status == "imported"
    assert again.fakturoid.expense_id == 99
    assert again.fakturoid.subject_id == 7
    assert again.fakturoid.imported_at == datetime(2024, 2, 1, tzinfo=UTC)


def test_update_status_missing_raises(tmp_path: Path) -> None:
    store = ExportStore(tmp_path)
    with pytest.raises(KeyError):
        store.update_status("nope", status="error")


# ---------- bad sidecar ----------


def test_records_raises_on_corrupt_sidecar(tmp_path: Path) -> None:
    (tmp_path / "junk_aaaaaaaa.json").write_text("not json", encoding="utf-8")
    store = ExportStore(tmp_path)
    with pytest.raises(ValueError, match="Failed to parse sidecar"):
        store.records()
