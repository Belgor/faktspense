"""Orchestrates the import of one invoice: vendor-match → build payload → POST → state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from .fakturoid.client import FakturoidClient
from .fakturoid.expenses import build_expense_payload, create_expense
from .fakturoid.subjects import SubjectStore
from .models import ExportRecord, FakturoidStatusValue, VendorInfo

VendorPromptAction = Literal["create", "map", "skip"]


class AlreadyImportedError(Exception):
    """Raised when attempting to re-import a record already marked imported."""


class VendorNotFoundError(Exception):
    """Raised when a vendor can't be matched and creation is not allowed."""


@dataclass(frozen=True)
class ImportFlags:
    dry_run: bool = False
    auto_create_subjects: bool = False
    no_create: bool = False
    refresh_subjects: bool = False


class VendorPrompt(Protocol):
    """Called when a vendor is not found in Fakturoid.

    Return one of:
      - ``("create", None)`` to create a new subject from extracted data
      - ``("map", subject_dict)`` to link to an existing subject
      - ``("skip", None)`` to skip the invoice
    """

    def __call__(
        self, vendor: VendorInfo, candidates: list[dict[str, Any]]
    ) -> tuple[VendorPromptAction, dict[str, Any] | None]: ...


@dataclass(frozen=True)
class ImportOutcome:
    status: FakturoidStatusValue
    subject_id: int | None
    expense_id: int | None
    imported_at: datetime | None
    error: str | None = None


class ImportRunner:
    def __init__(
        self,
        *,
        client: FakturoidClient,
        subjects: SubjectStore,
        pdf_root: Path,
        vendor_prompt: VendorPrompt,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._subjects = subjects
        self._pdf_root = pdf_root
        self._prompt = vendor_prompt
        self._now = now

    def run_one(self, record: ExportRecord, flags: ImportFlags) -> ImportOutcome:
        if record.fakturoid.status == "imported":
            raise AlreadyImportedError(
                f"Record {record.id!r} already imported "
                f"(expense_id={record.fakturoid.expense_id}, "
                f"imported_at={record.fakturoid.imported_at})"
            )

        if flags.refresh_subjects:
            self._subjects.refresh()

        subject = self._resolve_subject(record.vendor, flags)
        if subject is None:  # user chose to skip
            return ImportOutcome(
                status="skipped",
                subject_id=None,
                expense_id=None,
                imported_at=None,
            )
        subject_id = int(subject["id"])

        pdf_path = self._resolve_pdf_path(record.source_pdf)

        if flags.dry_run:
            build_expense_payload(
                record,
                subject_id=subject_id,
                pdf_bytes=pdf_path.read_bytes(),
                pdf_filename=pdf_path.name,
            )
            return ImportOutcome(
                status="pending",
                subject_id=subject_id,
                expense_id=None,
                imported_at=None,
            )

        response = create_expense(self._client, record, subject_id=subject_id, pdf_path=pdf_path)
        expense_id = int(response["id"])
        return ImportOutcome(
            status="imported",
            subject_id=subject_id,
            expense_id=expense_id,
            imported_at=self._now(),
        )

    # -------- helpers --------

    def _resolve_subject(self, vendor: VendorInfo, flags: ImportFlags) -> dict[str, Any] | None:
        if vendor.ico:
            hit = self._subjects.find_by_ico(vendor.ico)
            if hit is not None:
                return hit

        if flags.auto_create_subjects:
            return self._subjects.create(vendor)
        if flags.no_create:
            raise VendorNotFoundError(
                f"Vendor {vendor.name!r} (IČO={vendor.ico}) not found and --no-create set"
            )

        candidates = self._subjects.fuzzy_name_candidates(vendor.name) if vendor.name else []
        action, selected = self._prompt(vendor, candidates)
        if action == "create":
            return self._subjects.create(vendor)
        if action == "map" and selected is not None:
            return selected
        if action == "skip":
            return None
        raise RuntimeError(f"Unknown vendor-prompt action: {action!r}")

    def _resolve_pdf_path(self, source_pdf: str) -> Path:
        p = Path(source_pdf)
        candidates = [p] if p.is_absolute() else [self._pdf_root / p, p]
        for c in candidates:
            if c.exists():
                return c
        raise FileNotFoundError(f"PDF not found: tried {candidates}")
