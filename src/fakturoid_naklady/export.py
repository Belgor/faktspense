"""Read/write/merge helpers for export.json — the review artifact and state tracker."""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .models import ExportFile, ExportRecord, FakturoidStatus, FakturoidStatusValue


def load(path: Path) -> ExportFile:
    """Load export.json from disk, or return an empty file if it does not exist."""
    if not path.exists():
        return ExportFile(created_at=datetime.now(UTC))
    raw = path.read_text(encoding="utf-8")
    return ExportFile.model_validate_json(raw)


def save(path: Path, export: ExportFile) -> None:
    """Atomically write export.json (write to temp file in the same dir, then rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = export.model_dump_json(indent=2)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def find_by_id(export: ExportFile, invoice_id: str) -> ExportRecord | None:
    for rec in export.invoices:
        if rec.id == invoice_id:
            return rec
    return None


def upsert(export: ExportFile, record: ExportRecord) -> ExportFile:
    """Insert or replace a record by id, preserving existing fakturoid state on replace."""
    for i, existing in enumerate(export.invoices):
        if existing.id == record.id:
            merged = record.model_copy(update={"fakturoid": existing.fakturoid})
            export.invoices[i] = merged
            return export
    export.invoices.append(record)
    return export


def update_status(
    export: ExportFile,
    invoice_id: str,
    *,
    status: FakturoidStatusValue,
    subject_id: int | None = None,
    expense_id: int | None = None,
    imported_at: datetime | None = None,
    error: str | None = None,
) -> ExportRecord:
    rec = find_by_id(export, invoice_id)
    if rec is None:
        raise KeyError(f"No record with id={invoice_id!r}")
    rec.fakturoid = FakturoidStatus(
        subject_id=subject_id if subject_id is not None else rec.fakturoid.subject_id,
        expense_id=expense_id if expense_id is not None else rec.fakturoid.expense_id,
        imported_at=imported_at if imported_at is not None else rec.fakturoid.imported_at,
        status=status,
        error=error,
    )
    return rec
