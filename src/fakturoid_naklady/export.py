"""Per-invoice sidecar store — directory of ``<safe_pdf_stem>_<sha8>.json`` files.

Replaces the old bundled ``export.json`` layout. The user reviews/edits one
file per invoice; each file is a serialized :class:`ExportRecord` carrying
the full sha256 in its ``id`` field so re-runs can detect content changes.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from .models import ExportRecord, FakturoidStatus, FakturoidStatusValue

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def _safe_stem(stem: str) -> str:
    safe = _UNSAFE_CHARS.sub("_", stem).strip("._-")
    return safe or "invoice"


def sidecar_filename(record: ExportRecord) -> str:
    """Deterministic filename for one record's sidecar."""
    stem = Path(record.source_pdf).stem
    return f"{_safe_stem(stem)}_{record.id[:8]}.json"


def sha256_file(path: Path) -> str:
    """sha256 of a file's bytes — the durable identity of a record."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ExportStore:
    """Directory-backed persistence for :class:`ExportRecord` objects.

    One JSON file per record; ``id`` (full sha256 of pdf bytes) is the
    durable key. Writes are atomic (temp file + ``os.replace``); each
    mutating call saves immediately so a partial run is always safe to
    resume.

    A single instance caches the parsed sidecars in memory after first
    access, so a long batch run does not re-parse the directory on every
    lookup or status update. The cache assumes one process owns the
    directory for the lifetime of the instance.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._index: dict[str, ExportRecord] | None = None

    def records(self) -> list[ExportRecord]:
        """All records, sorted by ``(source_pdf, id)`` for deterministic output."""
        index = self._load_index()
        return sorted(index.values(), key=lambda r: (r.source_pdf, r.id))

    def find_by_id(self, invoice_id: str) -> ExportRecord | None:
        return self._load_index().get(invoice_id)

    def path_for(self, record: ExportRecord) -> Path:
        return self.root / sidecar_filename(record)

    def upsert(self, record: ExportRecord) -> ExportRecord:
        """Write the record's sidecar, preserving any prior fakturoid state.

        If a sidecar already exists for the same ``source_pdf`` but a
        different ``id`` (the PDF content changed), the stale sidecar is
        deleted so each PDF maps to at most one current sidecar.
        """
        existing = self.find_by_id(record.id)
        if existing is not None:
            record = record.model_copy(update={"fakturoid": existing.fakturoid})

        self._delete_stale_for_source(record.source_pdf, keep_id=record.id)
        self._write_sidecar(record)
        return record

    def update_status(
        self,
        invoice_id: str,
        *,
        status: FakturoidStatusValue,
        subject_id: int | None = None,
        expense_id: int | None = None,
        imported_at: datetime | None = None,
        error: str | None = None,
    ) -> ExportRecord:
        rec = self.find_by_id(invoice_id)
        if rec is None:
            raise KeyError(f"No record with id={invoice_id!r} in {self.root}")
        rec.fakturoid = FakturoidStatus(
            subject_id=subject_id if subject_id is not None else rec.fakturoid.subject_id,
            expense_id=expense_id if expense_id is not None else rec.fakturoid.expense_id,
            imported_at=imported_at if imported_at is not None else rec.fakturoid.imported_at,
            status=status,
            error=error,
            warnings=rec.fakturoid.warnings,
            sonnet_verdict=rec.fakturoid.sonnet_verdict,
        )
        self._write_sidecar(rec)
        return rec

    def _load_index(self) -> dict[str, ExportRecord]:
        if self._index is not None:
            return self._index
        index: dict[str, ExportRecord] = {}
        if self.root.is_dir():
            for path in sorted(self.root.glob("*.json")):
                if path.name.startswith("."):
                    continue
                try:
                    rec = ExportRecord.model_validate_json(path.read_text(encoding="utf-8"))
                except Exception as e:
                    raise ValueError(f"Failed to parse sidecar {path}: {e}") from e
                index[rec.id] = rec
        self._index = index
        return index

    def _write_sidecar(self, record: ExportRecord) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path_for(record)
        payload = record.model_dump_json(indent=2)
        fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(self.root))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, path)
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            raise
        self._load_index()[record.id] = record

    def _delete_stale_for_source(self, source_pdf: str, *, keep_id: str) -> None:
        index = self._load_index()
        stale = [
            (rec_id, rec)
            for rec_id, rec in index.items()
            if rec.source_pdf == source_pdf and rec_id != keep_id
        ]
        for rec_id, rec in stale:
            self.path_for(rec).unlink(missing_ok=True)
            del index[rec_id]
