"""End-to-end autotest against a real Fakturoid *test* account.

Runs the full extract → import → validate → idempotency loop on every PDF in a
folder (default: ``test_data_real/``) using real Anthropic and Fakturoid APIs.

Not a pytest test — invoked directly. Kept outside ``tests/`` so ``uv run pytest``
never hits the real APIs.

Credentials are provided as sbx sandbox secrets and injected into this
sandbox as TEST_-prefixed env vars (the prefix keeps them lexically
distinct from the unprefixed production vars the CLI reads):

    TEST_FAKTUROID_CLIENT_ID
    TEST_FAKTUROID_CLIENT_SECRET
    TEST_FAKTUROID_SLUG           (must be a dedicated test account)
    TEST_ANTHROPIC_API_KEY

Set on the host with ``sbx secret set <sandbox-name> <NAME> -t "..."``;
restart the sandbox to pick up newly-added or rotated secrets.

Usage:
    uv run python scripts/e2e_real.py
    uv run python scripts/e2e_real.py --cleanup        # delete created expenses+subjects
    uv run python scripts/e2e_real.py --skip-extract   # reuse existing export.json
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

# Make `src/` importable when running this file directly.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from fakturoid_naklady import export as export_mod  # noqa: E402
from fakturoid_naklady.extraction.claude import ClaudeExtractor  # noqa: E402
from fakturoid_naklady.extraction.renderer import render_pdf  # noqa: E402
from fakturoid_naklady.fakturoid.auth import OAuth2TokenProvider  # noqa: E402
from fakturoid_naklady.fakturoid.client import (  # noqa: E402
    USER_AGENT,
    FakturoidClient,
    FakturoidError,
)
from fakturoid_naklady.fakturoid.subjects import SubjectStore  # noqa: E402
from fakturoid_naklady.models import ExportRecord, VendorInfo  # noqa: E402
from fakturoid_naklady.pipeline import (  # noqa: E402
    AlreadyImportedError,
    ImportFlags,
    ImportRunner,
    VendorPromptAction,
)

# Credentials come from sbx sandbox secrets, injected as TEST_-prefixed env
# vars at sandbox startup. The prefix keeps them distinct from production vars.
ENV_PREFIX = "TEST_"
REQUIRED_ENV = (
    f"{ENV_PREFIX}FAKTUROID_CLIENT_ID",
    f"{ENV_PREFIX}FAKTUROID_CLIENT_SECRET",
    f"{ENV_PREFIX}FAKTUROID_SLUG",
    f"{ENV_PREFIX}ANTHROPIC_API_KEY",
)


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------


@dataclass
class RecordReport:
    invoice_number: str
    pdf_name: str
    extracted: bool = False
    imported: bool = False
    expense_id: int | None = None
    subject_id: int | None = None
    validation_errors: list[str] = field(default_factory=list)
    extraction_diffs: list[str] = field(default_factory=list)
    idempotency_ok: bool | None = None
    cleaned_up: bool = False
    hard_error: str | None = None

    @property
    def hard_ok(self) -> bool:
        return (
            self.extracted
            and self.imported
            and not self.validation_errors
            and self.idempotency_ok is not False
            and self.hard_error is None
        )


@dataclass
class Report:
    started_at: datetime
    records: dict[str, RecordReport] = field(default_factory=dict)
    created_subject_ids: set[int] = field(default_factory=set)

    def print_summary(self) -> int:
        print()
        print("=" * 78)
        print("E2E REAL-API RUN SUMMARY")
        print("=" * 78)
        hard_fails = 0
        for r in self.records.values():
            badge = "PASS" if r.hard_ok else "FAIL"
            if not r.hard_ok:
                hard_fails += 1
            print(f"[{badge}] {r.invoice_number:<30} {r.pdf_name}")
            if r.expense_id:
                print(f"        expense_id={r.expense_id}  subject_id={r.subject_id}")
            for err in r.validation_errors:
                print(f"        VALIDATION: {err}")
            if r.idempotency_ok is False:
                print("        IDEMPOTENCY: re-import did NOT refuse")
            if r.hard_error:
                print(f"        ERROR: {r.hard_error}")
            for d in r.extraction_diffs:
                print(f"        (soft) {d}")
        print("-" * 78)
        print(f"{len(self.records)} records, {hard_fails} hard failure(s)")
        return hard_fails


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_env() -> None:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)


def _build_fakturoid() -> tuple[FakturoidClient, httpx.Client]:
    http = httpx.Client(timeout=60.0)
    tp = OAuth2TokenProvider(
        client_id=os.environ[f"{ENV_PREFIX}FAKTUROID_CLIENT_ID"],
        client_secret=os.environ[f"{ENV_PREFIX}FAKTUROID_CLIENT_SECRET"],
        http=http,
        user_agent=USER_AGENT,
    )
    return FakturoidClient(
        slug=os.environ[f"{ENV_PREFIX}FAKTUROID_SLUG"],
        http=http,
        token_provider=tp,
    ), http


def _build_extractor() -> ClaudeExtractor:
    import anthropic

    # Pass the key explicitly — the SDK's default reads ANTHROPIC_API_KEY, which
    # we deliberately do NOT use (to avoid accidental production-key spend here).
    return ClaudeExtractor(
        client=anthropic.Anthropic(api_key=os.environ[f"{ENV_PREFIX}ANTHROPIC_API_KEY"])
    )


def _auto_create_prompt(
    vendor: VendorInfo, candidates: list[dict[str, Any]]
) -> tuple[VendorPromptAction, dict[str, Any] | None]:
    """Fallback prompt — should not be invoked when --auto-create-subjects is on."""
    return ("create", None)


# ----------------------------------------------------------------------------
# Phases
# ----------------------------------------------------------------------------


def phase_extract(pdf_dir: Path, export_path: Path, report: Report) -> None:
    print(f"\n[extract] scanning {pdf_dir}")
    pdfs = sorted(p for p in pdf_dir.glob("*.pdf") if p.is_file())
    if not pdfs:
        print(f"ERROR: no PDFs in {pdf_dir}", file=sys.stderr)
        sys.exit(1)

    extractor = _build_extractor()
    export = export_mod.load(export_path)

    for pdf in pdfs:
        invoice_id = _sha256_file(pdf)
        existing = export_mod.find_by_id(export, invoice_id)
        if existing is not None:
            report.records[invoice_id] = RecordReport(
                invoice_number=existing.invoice_number,
                pdf_name=pdf.name,
                extracted=True,
            )
            print(f"  skip {pdf.name} — already extracted")
            continue

        print(f"  extract {pdf.name}")
        try:
            rendered = render_pdf(pdf)
            extracted = extractor.extract(rendered)
        except Exception as e:
            report.records[invoice_id] = RecordReport(
                invoice_number=f"<{pdf.name}>",
                pdf_name=pdf.name,
                hard_error=f"extract failed: {e}",
            )
            continue

        record = ExportRecord.from_extraction(
            invoice_id=invoice_id,
            source_pdf=str(pdf.resolve()),
            extracted_at=datetime.now(UTC),
            extracted=extracted,
        )
        export_mod.upsert(export, record)
        export_mod.save(export_path, export)
        report.records[invoice_id] = RecordReport(
            invoice_number=record.invoice_number,
            pdf_name=pdf.name,
            extracted=True,
        )

    print(f"[extract] wrote {export_path}")


def phase_import(
    export_path: Path,
    report: Report,
    client: FakturoidClient,
    subjects: SubjectStore,
) -> None:
    print(f"\n[import] {export_path}")

    # Snapshot existing subject ids so cleanup can tell ours from theirs.
    pre_existing_subject_ids = subjects.loaded_subject_ids()

    export = export_mod.load(export_path)
    runner = ImportRunner(
        client=client,
        subjects=subjects,
        pdf_root=export_path.parent,
        vendor_prompt=_auto_create_prompt,
    )
    flags = ImportFlags(auto_create_subjects=True)

    for record in export.invoices:
        r = report.records.setdefault(
            record.id,
            RecordReport(
                invoice_number=record.invoice_number,
                pdf_name=Path(record.source_pdf).name,
            ),
        )
        if record.fakturoid.status == "imported":
            r.imported = True
            r.expense_id = record.fakturoid.expense_id
            r.subject_id = record.fakturoid.subject_id
            print(f"  skip {record.invoice_number} — already imported")
            continue
        print(f"  import {record.invoice_number}")
        try:
            outcome = runner.run_one(record, flags)
        except Exception as e:
            r.hard_error = f"import failed: {e}"
            export_mod.update_status(export, record.id, status="error", error=str(e))
            export_mod.save(export_path, export)
            continue

        export_mod.update_status(
            export,
            record.id,
            status=outcome.status,
            subject_id=outcome.subject_id,
            expense_id=outcome.expense_id,
            imported_at=outcome.imported_at,
        )
        export_mod.save(export_path, export)
        if outcome.status == "imported":
            r.imported = True
            r.expense_id = outcome.expense_id
            r.subject_id = outcome.subject_id
            if outcome.subject_id and outcome.subject_id not in pre_existing_subject_ids:
                report.created_subject_ids.add(outcome.subject_id)


def phase_validate(
    export_path: Path,
    report: Report,
    client: FakturoidClient,
) -> None:
    print("\n[validate] fetching each created expense back from Fakturoid")
    export = export_mod.load(export_path)
    for record in export.invoices:
        r = report.records.get(record.id)
        if r is None:
            continue
        if not r.imported or r.expense_id is None:
            continue
        try:
            resp = client.request("GET", client.account_url(f"/expenses/{r.expense_id}.json"))
            body = resp.json()
        except FakturoidError as e:
            r.validation_errors.append(f"GET expense/{r.expense_id} -> {e}")
            continue

        # --- hard assertions: wiring correctness ---
        if body.get("custom_id") != record.id:
            r.validation_errors.append(
                f"custom_id mismatch: expected {record.id!r}, got {body.get('custom_id')!r}"
            )
        if body.get("number") != record.invoice_number:
            r.validation_errors.append(
                f"number mismatch: expected {record.invoice_number!r}, got {body.get('number')!r}"
            )
        if body.get("subject_id") != r.subject_id:
            r.validation_errors.append(
                f"subject_id mismatch: expected {r.subject_id}, got {body.get('subject_id')}"
            )
        attachments = body.get("attachments") or []
        if not attachments:
            r.validation_errors.append("no PDF attachment present on expense")

        # --- soft diff: extraction quality (printed, not fatal) ---
        api_total = _parse_decimal(body.get("total"))
        if (
            record.total is not None
            and api_total is not None
            and abs(api_total - record.total) > Decimal("0.02")
        ):
            r.extraction_diffs.append(
                f"total differs: extracted={record.total}, Fakturoid={api_total}"
            )
        api_lines = body.get("lines") or []
        if len(api_lines) != len(record.lines):
            r.extraction_diffs.append(
                f"line count differs: extracted={len(record.lines)}, Fakturoid={len(api_lines)}"
            )


def phase_idempotency(
    export_path: Path,
    report: Report,
    client: FakturoidClient,
    subjects: SubjectStore,
) -> None:
    print("\n[idempotency] re-running import — every record should be refused")
    export = export_mod.load(export_path)
    runner = ImportRunner(
        client=client,
        subjects=subjects,
        pdf_root=export_path.parent,
        vendor_prompt=_auto_create_prompt,
    )
    flags = ImportFlags(auto_create_subjects=True)
    for record in export.invoices:
        r = report.records.get(record.id)
        if r is None or not r.imported:
            continue
        try:
            runner.run_one(record, flags)
        except AlreadyImportedError:
            r.idempotency_ok = True
            continue
        # Did NOT raise — record was either already-imported-fresh or worse.
        # ImportRunner refuses when status=='imported', so this is unexpected.
        r.idempotency_ok = False


def phase_cleanup(
    export_path: Path,
    report: Report,
    client: FakturoidClient,
) -> None:
    print("\n[cleanup] deleting created expenses + subjects")
    export = export_mod.load(export_path)

    # Delete expenses first (they reference subjects).
    for record in export.invoices:
        r = report.records.get(record.id)
        if r is None or r.expense_id is None:
            continue
        try:
            client.request("DELETE", client.account_url(f"/expenses/{r.expense_id}.json"))
            print(f"  deleted expense {r.expense_id} ({record.invoice_number})")
            r.cleaned_up = True
        except FakturoidError as e:
            print(f"  WARN: delete expense {r.expense_id} failed: {e}", file=sys.stderr)

    # Then subjects that this run created.
    for sid in sorted(report.created_subject_ids):
        try:
            client.request("DELETE", client.account_url(f"/subjects/{sid}.json"))
            print(f"  deleted subject {sid}")
        except FakturoidError as e:
            print(f"  WARN: delete subject {sid} failed: {e}", file=sys.stderr)


# ----------------------------------------------------------------------------
# Utils
# ----------------------------------------------------------------------------


def _parse_decimal(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Entrypoint
# ----------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pdf-dir",
        type=Path,
        default=REPO_ROOT / "test_data_real",
        help="Folder of PDFs to use as input (default: test_data_real/).",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="Scratch dir for export.json + subject cache (default: <pdf-dir>/_run/).",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete created expenses+subjects at the end.",
    )
    parser.add_argument("--skip-extract", action="store_true")
    parser.add_argument("--skip-import", action="store_true")
    parser.add_argument("--skip-validate", action="store_true")
    parser.add_argument("--skip-idempotency", action="store_true")
    args = parser.parse_args()

    _require_env()

    if not args.pdf_dir.is_dir():
        print(f"ERROR: {args.pdf_dir} is not a directory", file=sys.stderr)
        return 2

    work_dir = args.work_dir or (args.pdf_dir / "_run")
    work_dir.mkdir(parents=True, exist_ok=True)
    export_path = work_dir / "export.json"
    subjects_cache = work_dir / "subjects_cache.json"

    report = Report(started_at=datetime.now(UTC))

    client, http = _build_fakturoid()
    try:
        if not args.skip_extract:
            phase_extract(args.pdf_dir, export_path, report)
        else:
            export = export_mod.load(export_path)
            for rec in export.invoices:
                report.records.setdefault(
                    rec.id,
                    RecordReport(
                        invoice_number=rec.invoice_number,
                        pdf_name=Path(rec.source_pdf).name,
                        extracted=True,
                        imported=rec.fakturoid.status == "imported",
                        expense_id=rec.fakturoid.expense_id,
                        subject_id=rec.fakturoid.subject_id,
                    ),
                )

        subjects = SubjectStore(client=client, cache_path=subjects_cache)

        if not args.skip_import:
            phase_import(export_path, report, client, subjects)
        if not args.skip_validate:
            phase_validate(export_path, report, client)
        if not args.skip_idempotency:
            phase_idempotency(export_path, report, client, subjects)

        if args.cleanup:
            phase_cleanup(export_path, report, client)
    finally:
        http.close()

    hard_fails = report.print_summary()
    return 0 if hard_fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
