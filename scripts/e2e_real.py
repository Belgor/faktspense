"""Live e2e autotest against a real Fakturoid *test* account.

Drives the production faktspense CLI (faktspense extract + faktspense import)
against a dedicated test account, then verifies the results with direct
Fakturoid API calls. Every check is recorded in report.json; every Fakturoid
HTTP request is appended to api.log.

Intended workflow for an agent iterating on faktspense:

1. Make a code change.
2. Run `uv run python scripts/e2e_real.py --cleanup`.
3. Read test_data_real/_faktspense_run/report.json (or report.md).
4. Fix any failed checks; loop. Use --skip-extract to reuse sidecars when
   only fixing import or validation issues.

Not wired into pytest — `uv run pytest` never touches real APIs.

Credentials are read from TEST_-prefixed env vars. Load them from the
mounted secrets file before running:

    ls -l /home/belgor/.config/faktspense/.env.autotest
    mount | grep faktspense           # confirm ro virtiofs mount
    set -a; source /home/belgor/.config/faktspense/.env.autotest; set +a
    env | grep '^TEST_' | sed 's/=.*/=<set>/'

See docs/AUTOTEST.md for the full setup instructions.

Artifacts written to --work-dir (default test_data_real/_faktspense_run/):

    <pdf_stem>_<sha8>.json   one sidecar per PDF (same layout the CLI writes)
    .subjects_cache.json     isolated subject cache
    report.json              structured run log
    report.md                human-readable summary (extraction data + results)
    api.log                  one line per Fakturoid HTTP request

Usage:
    uv run python scripts/e2e_real.py
    uv run python scripts/e2e_real.py --cleanup
    uv run python scripts/e2e_real.py --verify          # Sonnet second-pass check
    uv run python scripts/e2e_real.py --skip-extract    # reuse existing sidecars
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from fakturoid_naklady.export import ExportStore, sha256_file  # noqa: E402
from fakturoid_naklady.fakturoid.auth import OAuth2TokenProvider  # noqa: E402
from fakturoid_naklady.fakturoid.client import (  # noqa: E402
    USER_AGENT,
    FakturoidClient,
    FakturoidError,
)
from fakturoid_naklady.fakturoid.subjects import SubjectStore  # noqa: E402
from fakturoid_naklady.models import ExportRecord, normalize_ico  # noqa: E402

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
    # Extraction data (from sidecar)
    invoice_number: str | None = None
    vendor_name: str | None = None
    vendor_ico: str | None = None
    vendor_dic: str | None = None
    issued_on: str | None = None
    due_date: str | None = None
    currency: str | None = None
    line_count: int = 0
    extracted_total: Decimal | None = None
    arithmetic_warnings: list[str] = field(default_factory=list)
    sonnet_ok: bool | None = None
    sonnet_issues: list[str] = field(default_factory=list)
    # Import state
    expense_id: int | None = None
    subject_id: int | None = None
    subject_was_created: bool = False
    # Quality
    extraction_diffs: list[str] = field(default_factory=list)
    # Checks + CLI output
    checks: list[Check] = field(default_factory=list)
    cli_extract_output: str = ""
    cli_import_output: str = ""

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
        print(
            "  Load with: set -a; source /home/belgor/.config/faktspense/.env.autotest; set +a",
            file=sys.stderr,
        )
        sys.exit(2)
    print("[env] credentials loaded:")
    for k in REQUIRED_ENV:
        suffix = f" ({os.environ[k]})" if k.endswith("_SLUG") else ""
        print(f"  {k}=<set>{suffix}")


def _build_cli_env() -> dict[str, str]:
    """Remap TEST_* vars to the unprefixed names the CLI expects."""
    env = os.environ.copy()
    env["FAKTUROID_CLIENT_ID"] = env[f"{ENV_PREFIX}FAKTUROID_CLIENT_ID"]
    env["FAKTUROID_CLIENT_SECRET"] = env[f"{ENV_PREFIX}FAKTUROID_CLIENT_SECRET"]
    env["FAKTUROID_SLUG"] = env[f"{ENV_PREFIX}FAKTUROID_SLUG"]
    env["ANTHROPIC_API_KEY"] = env[f"{ENV_PREFIX}ANTHROPIC_API_KEY"]
    return env


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


def _record_report_for(report: RunReport, record: ExportRecord, pdf_name: str) -> RecordReport:
    rr = report.records.get(record.id)
    if rr is None:
        rr = RecordReport(record_id=record.id, pdf_name=pdf_name)
        report.records[record.id] = rr
    return rr


def _populate_extraction_data(rr: RecordReport, record: ExportRecord) -> None:
    """Copy all extracted fields from a sidecar into RecordReport."""
    rr.invoice_number = record.invoice_number
    rr.vendor_name = record.vendor.name
    rr.vendor_ico = record.vendor.ico
    rr.vendor_dic = record.vendor.dic
    rr.issued_on = record.issued_on.isoformat() if record.issued_on else None
    rr.due_date = record.due_date.isoformat() if record.due_date else None
    rr.currency = record.currency
    rr.line_count = len(record.lines)
    rr.extracted_total = record.total
    rr.arithmetic_warnings = [w.message for w in record.fakturoid.warnings]
    if record.fakturoid.sonnet_verdict is not None:
        rr.sonnet_ok = record.fakturoid.sonnet_verdict.ok
        rr.sonnet_issues = list(record.fakturoid.sonnet_verdict.issues)
    if record.fakturoid.status == "imported":
        rr.expense_id = record.fakturoid.expense_id
        rr.subject_id = record.fakturoid.subject_id


def _run_cli(cmd: list[str], cli_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, env=cli_env, capture_output=True, text=True)
    for line in result.stdout.strip().splitlines():
        print(f"  {line}")
    for line in result.stderr.strip().splitlines():
        print(f"  STDERR: {line}", file=sys.stderr)
    return result


def _parse_decimal(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def _safe_normalize_ico(v: object) -> str | None:
    try:
        return normalize_ico(v)
    except (ValueError, TypeError):
        return None


# ----------------------------------------------------------------------------
# Phases
# ----------------------------------------------------------------------------


def phase_extract(
    pdf_dir: Path,
    work_dir: Path,
    report: RunReport,
    cli_env: dict[str, str],
    verify: bool,
) -> ExportStore:
    """Run `faktspense extract` via CLI; reload sidecars; populate report."""
    cmd = ["uv", "run", "faktspense", "extract", str(pdf_dir), "-o", str(work_dir)]
    if verify:
        cmd.append("--verify")
    print(f"\n[extract] scanning {pdf_dir}")

    pdfs = sorted(p for p in pdf_dir.glob("*.pdf") if p.is_file())
    if not pdfs:
        print(f"ERROR: no PDFs in {pdf_dir}", file=sys.stderr)
        sys.exit(1)

    result = _run_cli(cmd, cli_env)
    cli_output = (result.stdout + result.stderr).strip()

    store = ExportStore(work_dir)

    for pdf in pdfs:
        invoice_id = sha256_file(pdf)
        record = store.find_by_id(invoice_id)
        if record is None:
            rr = report.records.setdefault(
                invoice_id,
                RecordReport(record_id=invoice_id, pdf_name=pdf.name),
            )
            rr.cli_extract_output = cli_output
            rr.add(
                CHECK_EXTRACT,
                False,
                f"no sidecar written (CLI exit={result.returncode})",
            )
            continue

        rr = _record_report_for(report, record, pdf.name)
        _populate_extraction_data(rr, record)
        rr.cli_extract_output = cli_output
        note = "reused sidecar" if "already extracted" in result.stdout else "extracted"
        rr.add(
            CHECK_EXTRACT,
            True,
            f"{note}: {record.invoice_number} — {record.vendor.name!r}",
        )

    print(f"[extract] sidecars in {work_dir}")
    return store


def phase_import(
    store: ExportStore,
    report: RunReport,
    subjects: SubjectStore,
    cli_env: dict[str, str],
) -> ExportStore:
    """Run `faktspense import` via CLI; snapshot subjects for cleanup tracking."""
    print(f"\n[import] {store.root}")

    pre_subject_ids = subjects.loaded_subject_ids()

    cmd = [
        "uv",
        "run",
        "faktspense",
        "import",
        str(store.root),
        "--auto-create-subjects",
        "--force-review",
    ]
    result = _run_cli(cmd, cli_env)
    cli_output = (result.stdout + result.stderr).strip()

    # Refresh to discover subjects created by the CLI
    subjects.refresh()
    new_subject_ids = subjects.loaded_subject_ids() - pre_subject_ids
    for sid in sorted(new_subject_ids):
        if sid not in report.created_subject_ids:
            report.created_subject_ids.append(sid)

    store = ExportStore(store.root)

    for record in store.records():
        rr = _record_report_for(report, record, Path(record.source_pdf).name)
        _populate_extraction_data(rr, record)
        rr.cli_import_output = cli_output

        status = record.fakturoid.status
        if status == "imported":
            rr.subject_was_created = (
                record.fakturoid.subject_id is not None
                and record.fakturoid.subject_id in new_subject_ids
            )
            rr.add(
                CHECK_EXPENSE_CREATE,
                True,
                f"imported expense_id={rr.expense_id} subject_id={rr.subject_id}",
            )
        elif status == "error":
            rr.add(CHECK_EXPENSE_CREATE, False, f"import failed: {record.fakturoid.error}")
        else:
            rr.add(
                CHECK_EXPENSE_CREATE,
                False,
                f"status={status} (CLI exit={result.returncode})",
            )

    return store


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
                f"IČO mismatch (extracted={record.vendor.ico}, "
                f"Fakturoid={body.get('registration_no')})"
            )
        elif not rec_ico and not api_name:
            problems.append("no IČO and no name on subject")
        if rec_name and api_name and rec_name.lower() not in api_name.lower():
            # Soft: vendor names commonly differ in legal-form punctuation.
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
    cli_env: dict[str, str],
) -> None:
    """Re-run import; verify no expense_ids changed (no double-POST)."""
    print("\n[idempotency] re-running import — every record should be skipped")

    before = {
        rec.id: rec.fakturoid.expense_id
        for rec in store.records()
        if rec.fakturoid.status == "imported"
    }
    if not before:
        print("  no imported records to check")
        return

    cmd = [
        "uv",
        "run",
        "faktspense",
        "import",
        str(store.root),
        "--auto-create-subjects",
    ]
    _run_cli(cmd, cli_env)

    fresh = ExportStore(store.root)
    for rec in fresh.records():
        rr = report.records.get(rec.id)
        if rr is None or rec.id not in before:
            continue
        old_eid = before[rec.id]
        new_eid = rec.fakturoid.expense_id
        if new_eid == old_eid:
            rr.add(
                CHECK_IDEMPOTENCY,
                True,
                f"re-import skipped — expense_id unchanged ({old_eid})",
            )
        else:
            rr.add(
                CHECK_IDEMPOTENCY,
                False,
                f"expense_id changed {old_eid} → {new_eid} — possible double-import",
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


def _badge(rr: RecordReport, check_name: str) -> str:
    for c in rr.checks:
        if c.name == check_name:
            return "✓" if c.ok else "✗"
    return "—"


def write_markdown_report(path: Path, report: RunReport) -> None:
    started = report.started_at.isoformat()
    finished = report.finished_at.isoformat() if report.finished_at else "—"
    total = len(report.records)
    failures = report.hard_failures
    sorted_recs = sorted(report.records.values(), key=lambda r: r.pdf_name)

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
    ]

    # --- Section A: Extraction data ---
    lines += [
        "",
        "## Extracted invoice data",
        "",
        "| PDF | Vendor | IČO | Invoice # | Issued | Curr | Lines | Total | Warn |",
        "|-----|--------|-----|-----------|--------|------|-------|-------|------|",
    ]
    for rr in sorted_recs:
        if rr.arithmetic_warnings:
            warn = "⚠ arith"
        elif rr.sonnet_ok is False:
            warn = "✗ Sonnet"
        else:
            warn = "—"
        lines.append(
            f"| {rr.pdf_name} | {rr.vendor_name or '—'} | {rr.vendor_ico or '—'}"
            f" | {rr.invoice_number or '—'} | {rr.issued_on or '—'}"
            f" | {rr.currency or '—'} | {rr.line_count}"
            f" | {rr.extracted_total if rr.extracted_total is not None else '—'}"
            f" | {warn} |"
        )

    sonnet_rows = [rr for rr in sorted_recs if rr.sonnet_ok is not None]
    if sonnet_rows:
        lines += ["", "### Sonnet verification", ""]
        for rr in sonnet_rows:
            badge = "✓ ok" if rr.sonnet_ok else "✗ issues"
            issues = "; ".join(rr.sonnet_issues) if rr.sonnet_issues else "—"
            lines.append(f"- **{rr.invoice_number or rr.pdf_name}** — {badge}: {issues}")

    # --- Section B: Import results ---
    lines += [
        "",
        "## Import results",
        "",
        "| Invoice # | expense_id | subject_id | subject | Fakturoid total | Lines | Attachment |",
        "|-----------|-----------|-----------|---------|-----------------|-------|------------|",
    ]
    for rr in sorted_recs:
        exp_check = next((c for c in rr.checks if c.name == CHECK_VALIDATE_EXPENSE), None)
        fakt_total = fakt_lines = attachment = "—"
        if exp_check and exp_check.data:
            body = exp_check.data.get("fakturoid_response") or {}
            raw = _parse_decimal(body.get("total"))
            fakt_total = str(raw) if raw is not None else "—"
            fakt_lines = str(len(body.get("lines") or []))
            attachment = "✓" if body.get("attachments") else "✗"
        subject_note = "created" if rr.subject_was_created else "reused"
        lines.append(
            f"| {rr.invoice_number or '—'} | {rr.expense_id or '—'}"
            f" | {rr.subject_id or '—'} | {subject_note}"
            f" | {fakt_total} | {fakt_lines} | {attachment} |"
        )

    # --- Section C: Summary table ---
    lines += [
        "",
        "## Summary table",
        "",
        "| OK | Invoice # | PDF | extract | import"
        " | validate_expense | validate_subject | idempotency | Failed checks |",
        "|----|-----------|-----|---------|--------"
        "|------------------|------------------|-------------|---------------|",
    ]
    for rr in sorted_recs:
        ok_badge = "✓" if rr.hard_ok else "✗"
        failed = ", ".join(c.name for c in rr.checks if not c.ok) or "—"
        lines.append(
            f"| {ok_badge} | {rr.invoice_number or '—'} | {rr.pdf_name}"
            f" | {_badge(rr, CHECK_EXTRACT)}"
            f" | {_badge(rr, CHECK_EXPENSE_CREATE)}"
            f" | {_badge(rr, CHECK_VALIDATE_EXPENSE)}"
            f" | {_badge(rr, CHECK_VALIDATE_SUBJECT)}"
            f" | {_badge(rr, CHECK_IDEMPOTENCY)}"
            f" | {failed} |"
        )

    # --- Section D: Extraction-quality diffs ---
    soft = [(rr, rr.extraction_diffs) for rr in report.records.values() if rr.extraction_diffs]
    if soft:
        lines += ["", "## Extraction-quality diffs (non-fatal)", ""]
        for rr, diffs in sorted(soft, key=lambda x: x[0].pdf_name):
            lines.append(f"### {rr.invoice_number or rr.pdf_name}")
            for d in diffs:
                lines.append(f"- {d}")
            lines.append("")

    # --- Section E: Hard failure details ---
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
            if rr.arithmetic_warnings:
                lines.append("- arithmetic warnings:")
                for w in rr.arithmetic_warnings:
                    lines.append(f"  - {w}")
            if rr.extraction_diffs:
                lines.append("- soft diffs:")
                for d in rr.extraction_diffs:
                    lines.append(f"  - {d}")
            if rr.cli_extract_output:
                lines.append(f"- CLI extract output: `{rr.cli_extract_output[:400]}`")
            if rr.cli_import_output:
                lines.append(f"- CLI import output: `{rr.cli_import_output[:400]}`")
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
    parser.add_argument(
        "--cleanup", action="store_true", help="Delete created expenses + subjects at the end."
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Pass --verify to faktspense extract (Sonnet second-pass semantic check).",
    )
    parser.add_argument(
        "--skip-extract",
        action="store_true",
        help="Skip extraction; reuse sidecars already in work-dir.",
    )
    parser.add_argument(
        "--skip-import",
        action="store_true",
        help="Skip import; assume sidecars already have expense_ids.",
    )
    parser.add_argument(
        "--skip-validate",
        action="store_true",
        help="Skip GET-back validation of expenses and subjects.",
    )
    parser.add_argument(
        "--skip-idempotency", action="store_true", help="Skip idempotency re-run check."
    )
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

    cli_env = _build_cli_env()

    report = RunReport(
        started_at=datetime.now(UTC),
        pdf_dir=str(args.pdf_dir),
        work_dir=str(work_dir),
        fakturoid_slug=os.environ[f"{ENV_PREFIX}FAKTUROID_SLUG"],
        args={
            "cleanup": args.cleanup,
            "verify": args.verify,
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
            subjects = SubjectStore(client=client, cache_path=subjects_cache)

            if not args.skip_extract:
                store = phase_extract(args.pdf_dir, work_dir, report, cli_env, args.verify)
            else:
                store = ExportStore(work_dir)
                for rec in store.records():
                    rr = _record_report_for(report, rec, Path(rec.source_pdf).name)
                    _populate_extraction_data(rr, rec)
                    rr.add(CHECK_EXTRACT, True, "skipped (--skip-extract); reusing sidecar")

            if not args.skip_import:
                store = phase_import(store, report, subjects, cli_env)
            else:
                store = ExportStore(work_dir)
                for rec in store.records():
                    rr = _record_report_for(report, rec, Path(rec.source_pdf).name)
                    _populate_extraction_data(rr, rec)
                    if rec.fakturoid.status == "imported":
                        rr.add(
                            CHECK_EXPENSE_CREATE,
                            True,
                            f"skipped (--skip-import); "
                            f"already imported (expense_id={rr.expense_id})",
                        )

            if not args.skip_validate:
                phase_validate_expenses(store, report, client)
                phase_validate_subjects(store, report, client)
            if not args.skip_idempotency:
                phase_idempotency(store, report, cli_env)

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
