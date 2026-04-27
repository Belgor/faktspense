"""Live e2e autotest against a real Fakturoid *test* account.

Designed as an iteration tool for agentic development. The script really
extracts invoice data from PDFs, really creates subjects + expenses in
Fakturoid, and then GETs each one back to verify the data is there and
correct. Every check produces a structured event in ``report.json``;
every Fakturoid HTTP request is appended to ``api.log``. An agent reading
those artifacts can diagnose what went wrong without re-running the
script.

Not a pytest test — invoked directly. Kept outside ``tests/`` so
``uv run pytest`` never hits the real APIs.

Persistence goes through the production :class:`ExportStore`, so this
script exercises the same per-invoice sidecar layout the CLI uses.

Credentials are read from TEST_-prefixed env vars (the prefix keeps
them lexically distinct from the unprefixed production vars the CLI
reads):

    TEST_FAKTUROID_CLIENT_ID
    TEST_FAKTUROID_CLIENT_SECRET
    TEST_FAKTUROID_SLUG           (must be a dedicated test account)
    TEST_ANTHROPIC_API_KEY

See ``docs/AUTOTEST.md`` for the recommended setup: keep them in
``~/.config/faktspense/.env.autotest`` on the host, mount the dir
read-only into the sandbox via ``sbx run claude . ~/.config/faktspense:ro``,
then ``set -a; source ~/.config/faktspense/.env.autotest; set +a``
before running this script.

Artifacts written to ``--work-dir`` (default
``test_data_real/_faktspense_run/``):

    <pdf_stem>_<sha8>.json   one sidecar per PDF (production layout)
    .subjects_cache.json     isolated subject cache
    report.json              structured run log — one record entry per
                             invoice with pass/fail per check, full
                             Fakturoid response bodies, extraction diffs
    report.md                human-readable summary of the same data
    api.log                  one line per Fakturoid HTTP request

Usage:
    uv run python scripts/e2e_real.py
    uv run python scripts/e2e_real.py --cleanup
    uv run python scripts/e2e_real.py --skip-extract     # reuse sidecars
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

# Make `src/` importable when running this file directly.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from fakturoid_naklady.export import ExportStore, sha256_file  # noqa: E402
from fakturoid_naklady.extraction.claude import ClaudeExtractor  # noqa: E402
from fakturoid_naklady.extraction.renderer import render_pdf  # noqa: E402
from fakturoid_naklady.fakturoid.auth import OAuth2TokenProvider  # noqa: E402
from fakturoid_naklady.fakturoid.client import (  # noqa: E402
    USER_AGENT,
    FakturoidClient,
    FakturoidError,
)
from fakturoid_naklady.fakturoid.subjects import SubjectStore  # noqa: E402
from fakturoid_naklady.models import ExportRecord, VendorInfo, normalize_ico  # noqa: E402
from fakturoid_naklady.pipeline import (  # noqa: E402
    AlreadyImportedError,
    ImportFlags,
    ImportRunner,
    VendorPromptAction,
)

ENV_PREFIX = "TEST_"
REQUIRED_ENV = (
    f"{ENV_PREFIX}FAKTUROID_CLIENT_ID",
    f"{ENV_PREFIX}FAKTUROID_CLIENT_SECRET",
    f"{ENV_PREFIX}FAKTUROID_SLUG",
    f"{ENV_PREFIX}ANTHROPIC_API_KEY",
)

CHECK_EXTRACT = "extract"
CHECK_EXPENSE_CREATE = "expense_create"
CHECK_VALIDATE_EXPENSE = "validate_expense"
CHECK_VALIDATE_SUBJECT = "validate_subject"
CHECK_IDEMPOTENCY = "idempotency"
CHECK_CLEANUP_EXPENSE = "cleanup_expense"


# ----------------------------------------------------------------------------
# Report types
# ----------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    data: dict[str, Any] | None = None


@dataclass
class RecordReport:
    record_id: str
    pdf_name: str
    invoice_number: str | None = None
    vendor_name: str | None = None
    vendor_ico: str | None = None
    expense_id: int | None = None
    subject_id: int | None = None
    subject_was_created: bool = False
    extraction_diffs: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)

    @property
    def hard_ok(self) -> bool:
        if not self.checks:
            return False
        return all(c.ok for c in self.checks)

    def add(
        self, name: str, ok: bool, detail: str = "", data: dict[str, Any] | None = None
    ) -> Check:
        check = Check(name=name, ok=ok, detail=detail, data=data)
        self.checks.append(check)
        return check


@dataclass
class RunReport:
    started_at: datetime
    finished_at: datetime | None = None
    pdf_dir: str = ""
    work_dir: str = ""
    fakturoid_slug: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    records: dict[str, RecordReport] = field(default_factory=dict)
    created_subject_ids: list[int] = field(default_factory=list)

    @property
    def hard_failures(self) -> list[RecordReport]:
        return [r for r in self.records.values() if not r.hard_ok]


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _require_env() -> None:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)


def _build_http(log_fh: Any) -> httpx.Client:
    def on_request(req: httpx.Request) -> None:
        req.extensions["_t0"] = time.monotonic()

    def on_response(resp: httpx.Response) -> None:
        t0 = resp.request.extensions.get("_t0", time.monotonic())
        ms = int((time.monotonic() - t0) * 1000)
        ts = datetime.now(UTC).isoformat()
        line = f"{ts} {resp.request.method} {resp.request.url} -> {resp.status_code} ({ms}ms)\n"
        log_fh.write(line)
        log_fh.flush()

    return httpx.Client(
        timeout=60.0,
        event_hooks={"request": [on_request], "response": [on_response]},
    )


def _build_fakturoid(http: httpx.Client) -> FakturoidClient:
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
    )


def _build_extractor() -> ClaudeExtractor:
    import anthropic

    return ClaudeExtractor(
        client=anthropic.Anthropic(api_key=os.environ[f"{ENV_PREFIX}ANTHROPIC_API_KEY"])
    )


def _auto_create_prompt(
    vendor: VendorInfo, candidates: list[dict[str, Any]]
) -> tuple[VendorPromptAction, dict[str, Any] | None]:
    return ("create", None)


def _record_report_for(report: RunReport, record: ExportRecord, pdf_name: str) -> RecordReport:
    rr = report.records.get(record.id)
    if rr is None:
        rr = RecordReport(
            record_id=record.id,
            pdf_name=pdf_name,
            invoice_number=record.invoice_number,
            vendor_name=record.vendor.name,
            vendor_ico=record.vendor.ico,
        )
        report.records[record.id] = rr
    else:
        rr.invoice_number = record.invoice_number
        rr.vendor_name = record.vendor.name
        rr.vendor_ico = record.vendor.ico
    return rr


def _parse_decimal(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def _safe_normalize_ico(v: object) -> str | None:
    """Like models.normalize_ico but returns None on non-digit input rather than raising."""
    try:
        return normalize_ico(v)
    except (ValueError, TypeError):
        return None


# ----------------------------------------------------------------------------
# Phases
# ----------------------------------------------------------------------------


def phase_extract(pdf_dir: Path, store: ExportStore, report: RunReport) -> None:
    print(f"\n[extract] scanning {pdf_dir}")
    pdfs = sorted(p for p in pdf_dir.glob("*.pdf") if p.is_file())
    if not pdfs:
        print(f"ERROR: no PDFs in {pdf_dir}", file=sys.stderr)
        sys.exit(1)

    extractor = _build_extractor()

    for pdf in pdfs:
        invoice_id = sha256_file(pdf)
        existing = store.find_by_id(invoice_id)
        if existing is not None:
            rr = _record_report_for(report, existing, pdf.name)
            rr.add(CHECK_EXTRACT, True, "reused existing sidecar (sha256 matches)")
            print(f"  skip {pdf.name} — already extracted")
            continue

        print(f"  extract {pdf.name}")
        try:
            rendered = render_pdf(pdf)
            extracted = extractor.extract(rendered)
        except Exception as e:
            rr = report.records.setdefault(
                invoice_id,
                RecordReport(record_id=invoice_id, pdf_name=pdf.name),
            )
            rr.add(CHECK_EXTRACT, False, f"extract failed: {e}")
            continue

        record = ExportRecord.from_extraction(
            invoice_id=invoice_id,
            source_pdf=str(pdf.resolve()),
            extracted_at=datetime.now(UTC),
            extracted=extracted,
        )
        store.upsert(record)
        rr = _record_report_for(report, record, pdf.name)
        rr.add(
            CHECK_EXTRACT,
            True,
            f"extracted invoice_number={record.invoice_number} vendor={record.vendor.name}",
        )

    print(f"[extract] sidecars in {store.root}")


def phase_import(
    store: ExportStore,
    report: RunReport,
    client: FakturoidClient,
    subjects: SubjectStore,
) -> None:
    print(f"\n[import] {store.root}")

    pre_existing_subject_ids = subjects.loaded_subject_ids()
    runner = ImportRunner(
        client=client,
        subjects=subjects,
        pdf_root=store.root,
        vendor_prompt=_auto_create_prompt,
    )
    flags = ImportFlags(auto_create_subjects=True)

    for record in store.records():
        rr = _record_report_for(report, record, Path(record.source_pdf).name)
        if record.fakturoid.status == "imported":
            rr.expense_id = record.fakturoid.expense_id
            rr.subject_id = record.fakturoid.subject_id
            rr.add(
                CHECK_EXPENSE_CREATE,
                True,
                f"already imported (expense_id={rr.expense_id})",
            )
            print(f"  skip {record.invoice_number} — already imported")
            continue

        print(f"  import {record.invoice_number}")
        try:
            outcome = runner.run_one(record, flags)
        except Exception as e:
            rr.add(CHECK_EXPENSE_CREATE, False, f"import failed: {e}")
            store.update_status(record.id, status="error", error=str(e))
            continue

        store.update_status(
            record.id,
            status=outcome.status,
            subject_id=outcome.subject_id,
            expense_id=outcome.expense_id,
            imported_at=outcome.imported_at,
        )
        rr.subject_id = outcome.subject_id
        rr.expense_id = outcome.expense_id
        if outcome.subject_id and outcome.subject_id not in pre_existing_subject_ids:
            rr.subject_was_created = True
            if outcome.subject_id not in report.created_subject_ids:
                report.created_subject_ids.append(outcome.subject_id)
        ok = outcome.status == "imported"
        rr.add(
            CHECK_EXPENSE_CREATE,
            ok,
            f"status={outcome.status} "
            f"expense_id={outcome.expense_id} subject_id={outcome.subject_id}",
        )


def phase_validate_expenses(
    store: ExportStore,
    report: RunReport,
    client: FakturoidClient,
) -> None:
    print("\n[validate] GET each created expense back from Fakturoid")
    for record in store.records():
        rr = report.records.get(record.id)
        if rr is None or rr.expense_id is None:
            continue
        try:
            resp = client.request("GET", client.account_url(f"/expenses/{rr.expense_id}.json"))
            body = resp.json()
        except FakturoidError as e:
            rr.add(CHECK_VALIDATE_EXPENSE, False, f"GET expense/{rr.expense_id} -> {e}")
            continue

        problems: list[str] = []
        if body.get("custom_id") != record.id:
            problems.append(
                f"custom_id mismatch (expected {record.id!r}, got {body.get('custom_id')!r})"
            )
        if body.get("original_number") != record.invoice_number:
            problems.append(
                f"original_number mismatch (expected {record.invoice_number!r}, "
                f"got {body.get('original_number')!r})"
            )
        if body.get("subject_id") != rr.subject_id:
            problems.append(
                f"subject_id mismatch (expected {rr.subject_id}, got {body.get('subject_id')})"
            )
        if not (body.get("attachments") or []):
            problems.append("no PDF attachment present")

        api_total = _parse_decimal(body.get("total"))
        if (
            record.total is not None
            and api_total is not None
            and abs(api_total - record.total) > Decimal("0.02")
        ):
            rr.extraction_diffs.append(f"total: extracted={record.total}, Fakturoid={api_total}")
        api_lines = body.get("lines") or []
        if len(api_lines) != len(record.lines):
            rr.extraction_diffs.append(
                f"line count: extracted={len(record.lines)}, Fakturoid={len(api_lines)}"
            )

        ok = not problems
        detail = "expense matches sidecar" if ok else "; ".join(problems)
        rr.add(CHECK_VALIDATE_EXPENSE, ok, detail, data={"fakturoid_response": body})


def phase_validate_subjects(
    store: ExportStore,
    report: RunReport,
    client: FakturoidClient,
) -> None:
    print("\n[validate] GET each subject back from Fakturoid")
    seen: dict[int, dict[str, Any]] = {}
    for record in store.records():
        rr = report.records.get(record.id)
        if rr is None or rr.subject_id is None:
            continue
        sid = rr.subject_id

        if sid in seen:
            body = seen[sid]
        else:
            try:
                resp = client.request("GET", client.account_url(f"/subjects/{sid}.json"))
                body = resp.json()
                seen[sid] = body
            except FakturoidError as e:
                rr.add(CHECK_VALIDATE_SUBJECT, False, f"GET subject/{sid} -> {e}")
                continue

        api_ico = _safe_normalize_ico(body.get("registration_no"))
        rec_ico = _safe_normalize_ico(record.vendor.ico)
        api_name = (body.get("name") or "").strip()
        rec_name = (record.vendor.name or "").strip()

        problems: list[str] = []
        if rec_ico and api_ico != rec_ico:
            problems.append(
                "IČO mismatch "
                f"(extracted={record.vendor.ico}, "
                f"Fakturoid={body.get('registration_no')})"
            )
        elif not rec_ico and not api_name:
            problems.append("no IČO and no name on subject")
        if rec_name and api_name and rec_name.lower() not in api_name.lower():
            # Soft: vendor names commonly differ in legal-form punctuation
            # (e.g. "s.r.o." vs "s. r. o."). Don't fail the check on this.
            rr.extraction_diffs.append(
                f"vendor name: extracted={rec_name!r}, Fakturoid={api_name!r}"
            )

        ok = not problems
        action = "created" if rr.subject_was_created else "reused"
        detail = (
            f"subject {sid} ({action}) matches IČO {record.vendor.ico}"
            if ok
            else "; ".join(problems)
        )
        rr.add(
            CHECK_VALIDATE_SUBJECT,
            ok,
            detail,
            data={"subject_id": sid, "fakturoid_response": body, "action": action},
        )


def phase_idempotency(
    store: ExportStore,
    report: RunReport,
    client: FakturoidClient,
    subjects: SubjectStore,
) -> None:
    print("\n[idempotency] re-running import — every record should be refused")
    runner = ImportRunner(
        client=client,
        subjects=subjects,
        pdf_root=store.root,
        vendor_prompt=_auto_create_prompt,
    )
    flags = ImportFlags(auto_create_subjects=True)
    for record in store.records():
        rr = report.records.get(record.id)
        if rr is None or rr.expense_id is None:
            continue
        try:
            runner.run_one(record, flags)
        except AlreadyImportedError:
            rr.add(CHECK_IDEMPOTENCY, True, "re-import correctly raised AlreadyImportedError")
            continue
        rr.add(
            CHECK_IDEMPOTENCY,
            False,
            "re-import did NOT raise AlreadyImportedError — duplicate POST risk",
        )


def phase_cleanup(
    store: ExportStore,
    report: RunReport,
    client: FakturoidClient,
) -> None:
    print("\n[cleanup] deleting created expenses + subjects")

    for record in store.records():
        rr = report.records.get(record.id)
        if rr is None or rr.expense_id is None:
            continue
        try:
            client.request("DELETE", client.account_url(f"/expenses/{rr.expense_id}.json"))
            rr.add(CHECK_CLEANUP_EXPENSE, True, f"deleted expense {rr.expense_id}")
            print(f"  deleted expense {rr.expense_id} ({record.invoice_number})")
        except FakturoidError as e:
            rr.add(CHECK_CLEANUP_EXPENSE, False, f"delete expense {rr.expense_id} -> {e}")
            print(f"  WARN: delete expense {rr.expense_id} failed: {e}", file=sys.stderr)

    for sid in sorted(report.created_subject_ids):
        try:
            client.request("DELETE", client.account_url(f"/subjects/{sid}.json"))
            print(f"  deleted subject {sid}")
        except FakturoidError as e:
            print(f"  WARN: delete subject {sid} failed: {e}", file=sys.stderr)


# ----------------------------------------------------------------------------
# Report writers
# ----------------------------------------------------------------------------


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"Cannot serialize {type(o).__name__}")


def write_json_report(path: Path, report: RunReport) -> None:
    payload = asdict(report)
    path.write_text(
        json.dumps(payload, indent=2, default=_json_default, ensure_ascii=False),
        encoding="utf-8",
    )


def write_markdown_report(path: Path, report: RunReport) -> None:
    started = report.started_at.isoformat()
    finished = report.finished_at.isoformat() if report.finished_at else "—"
    total = len(report.records)
    failures = report.hard_failures
    lines: list[str] = [
        "# faktspense e2e run",
        "",
        f"- Started:  {started}",
        f"- Finished: {finished}",
        f"- Slug:     {report.fakturoid_slug}",
        f"- PDF dir:  {report.pdf_dir}",
        f"- Work dir: {report.work_dir}",
        f"- Records:  {total}",
        f"- Hard failures: {len(failures)}",
        "",
        "## Summary table",
        "",
        "| OK | Invoice | PDF | expense_id | subject_id | Failed checks |",
        "|----|---------|-----|------------|------------|---------------|",
    ]
    for rr in sorted(report.records.values(), key=lambda r: r.pdf_name):
        badge = "✓" if rr.hard_ok else "✗"
        failed = ", ".join(c.name for c in rr.checks if not c.ok) or "—"
        lines.append(
            f"| {badge} | {rr.invoice_number or '—'} | {rr.pdf_name} | "
            f"{rr.expense_id or '—'} | {rr.subject_id or '—'} | {failed} |"
        )

    if failures:
        lines += ["", "## Failures (details)", ""]
        for rr in failures:
            lines.append(f"### {rr.invoice_number or rr.pdf_name}")
            lines.append("")
            lines.append(f"- record_id: `{rr.record_id}`")
            lines.append(f"- pdf: `{rr.pdf_name}`")
            if rr.expense_id:
                lines.append(f"- expense_id: {rr.expense_id}")
            if rr.subject_id:
                lines.append(f"- subject_id: {rr.subject_id}")
            for c in rr.checks:
                mark = "✓" if c.ok else "✗"
                lines.append(f"- {mark} **{c.name}** — {c.detail}")
            if rr.extraction_diffs:
                lines.append("- soft diffs:")
                for d in rr.extraction_diffs:
                    lines.append(f"  - {d}")
            lines.append("")

    soft = [(rr, rr.extraction_diffs) for rr in report.records.values() if rr.extraction_diffs]
    if soft:
        lines += ["", "## Extraction-quality diffs (non-fatal)", ""]
        for rr, diffs in soft:
            lines.append(f"### {rr.invoice_number or rr.pdf_name}")
            for d in diffs:
                lines.append(f"- {d}")
            lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def print_summary(report: RunReport, report_json: Path, report_md: Path) -> int:
    failures = report.hard_failures
    print()
    print("=" * 78)
    print("E2E REAL-API RUN SUMMARY")
    print("=" * 78)
    for rr in sorted(report.records.values(), key=lambda r: r.pdf_name):
        badge = "PASS" if rr.hard_ok else "FAIL"
        print(f"[{badge}] {(rr.invoice_number or '—'):<30} {rr.pdf_name}")
        for c in rr.checks:
            if not c.ok:
                print(f"        ✗ {c.name}: {c.detail}")
        for d in rr.extraction_diffs:
            print(f"        (soft) {d}")
    print("-" * 78)
    print(f"{len(report.records)} records, {len(failures)} hard failure(s)")
    print(f"  report (json): {report_json}")
    print(f"  report (md):   {report_md}")
    return len(failures)


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
        help=(
            "Scratch dir for sidecars + subject cache + reports "
            "(default: <pdf-dir>/_faktspense_run/)."
        ),
    )
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--skip-extract", action="store_true")
    parser.add_argument("--skip-import", action="store_true")
    parser.add_argument("--skip-validate", action="store_true")
    parser.add_argument("--skip-idempotency", action="store_true")
    args = parser.parse_args()

    _require_env()

    if not args.pdf_dir.is_dir():
        print(f"ERROR: {args.pdf_dir} is not a directory", file=sys.stderr)
        return 2

    work_dir = args.work_dir or (args.pdf_dir / "_faktspense_run")
    work_dir.mkdir(parents=True, exist_ok=True)
    subjects_cache = work_dir / ".subjects_cache.json"
    api_log = work_dir / "api.log"
    report_json = work_dir / "report.json"
    report_md = work_dir / "report.md"
    store = ExportStore(work_dir)

    report = RunReport(
        started_at=datetime.now(UTC),
        pdf_dir=str(args.pdf_dir),
        work_dir=str(work_dir),
        fakturoid_slug=os.environ[f"{ENV_PREFIX}FAKTUROID_SLUG"],
        args={
            "cleanup": args.cleanup,
            "skip_extract": args.skip_extract,
            "skip_import": args.skip_import,
            "skip_validate": args.skip_validate,
            "skip_idempotency": args.skip_idempotency,
        },
    )

    with api_log.open("w", encoding="utf-8") as log_fh:
        http = _build_http(log_fh)
        try:
            client = _build_fakturoid(http)
            if not args.skip_extract:
                phase_extract(args.pdf_dir, store, report)
            else:
                for rec in store.records():
                    rr = _record_report_for(report, rec, Path(rec.source_pdf).name)
                    rr.add(CHECK_EXTRACT, True, "skipped (--skip-extract); reusing sidecar")
                    if rec.fakturoid.status == "imported":
                        rr.expense_id = rec.fakturoid.expense_id
                        rr.subject_id = rec.fakturoid.subject_id

            subjects = SubjectStore(client=client, cache_path=subjects_cache)

            if not args.skip_import:
                phase_import(store, report, client, subjects)
            if not args.skip_validate:
                phase_validate_expenses(store, report, client)
                phase_validate_subjects(store, report, client)
            if not args.skip_idempotency:
                phase_idempotency(store, report, client, subjects)

            if args.cleanup:
                phase_cleanup(store, report, client)
        finally:
            report.finished_at = datetime.now(UTC)
            write_json_report(report_json, report)
            write_markdown_report(report_md, report)
            http.close()

    return 0 if print_summary(report, report_json, report_md) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
