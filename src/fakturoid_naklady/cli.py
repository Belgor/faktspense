"""faktspense CLI: extract / import / status."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.prompt import IntPrompt
from rich.table import Table

from .export import ExportStore, sha256_file
from .extraction.claude import ClaudeExtractor
from .extraction.renderer import render_pdf
from .fakturoid.auth import OAuth2TokenProvider
from .fakturoid.client import USER_AGENT, FakturoidClient
from .fakturoid.subjects import SubjectStore
from .models import ExportRecord, VendorInfo
from .pipeline import (
    AlreadyImportedError,
    ImportFlags,
    ImportRunner,
    VendorNotFoundError,
    VendorPromptAction,
)

app = typer.Typer(
    help="Extract invoice data from PDFs and import them as expenses into Fakturoid.",
    no_args_is_help=True,
)
console = Console()
err_console = Console(stderr=True)


# -------- helpers --------


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        err_console.print(f"[red]Missing required env var: {name}[/red]")
        raise typer.Exit(code=2)
    return val


def _iter_pdfs(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(p for p in path.glob("*.pdf") if p.is_file())
    raise typer.BadParameter(f"Not a PDF file or directory: {path}")


def _build_fakturoid() -> tuple[FakturoidClient, httpx.Client]:
    slug = _require_env("FAKTUROID_SLUG")
    client_id = _require_env("FAKTUROID_CLIENT_ID")
    client_secret = _require_env("FAKTUROID_CLIENT_SECRET")
    http = httpx.Client(timeout=30.0)
    tp = OAuth2TokenProvider(
        client_id=client_id,
        client_secret=client_secret,
        http=http,
        user_agent=USER_AGENT,
    )
    return FakturoidClient(slug=slug, http=http, token_provider=tp), http


def _build_extractor() -> ClaudeExtractor:
    import anthropic  # lazy import

    _require_env("ANTHROPIC_API_KEY")
    anth = anthropic.Anthropic()
    return ClaudeExtractor(client=anth)


def _vendor_prompt(
    vendor: VendorInfo, candidates: list[dict[str, Any]]
) -> tuple[VendorPromptAction, dict[str, Any] | None]:
    console.print()
    console.print("[yellow]⚠  Vendor not found in Fakturoid[/yellow]")
    console.print(
        f"   Extracted:  [bold]{vendor.name}[/bold]   IČO: {vendor.ico or '—'}   "
        f"DIČ: {vendor.dic or '—'}"
    )
    console.print()

    options: list[tuple[VendorPromptAction, dict[str, Any] | None]] = [("create", None)]
    console.print("   [1] Create new subject from extracted data  (default)")
    for cand in candidates:
        label = f"{cand.get('name', '')!r} (IČO: {cand.get('registration_no') or '—'})"
        console.print(f"   [{len(options) + 1}] Map to existing: {label}")
        options.append(("map", cand))
    console.print(f"   [{len(options) + 1}] Skip this invoice")
    options.append(("skip", None))

    choice = IntPrompt.ask("Choice", default=1, choices=[str(i + 1) for i in range(len(options))])
    return options[choice - 1]


# -------- commands --------


@app.command()
def extract(
    input_path: Path = typer.Argument(..., exists=True, help="PDF file or directory."),
    output: Path = typer.Option(
        Path("export"), "--output", "-o", help="Directory for per-invoice JSON sidecars."
    ),
) -> None:
    """Extract invoice data from PDF(s); writes one JSON sidecar per PDF into ``output``."""
    pdfs = _iter_pdfs(input_path)
    if not pdfs:
        err_console.print("[red]No PDFs found.[/red]")
        raise typer.Exit(code=1)

    extractor = _build_extractor()
    store = ExportStore(output)

    for pdf in pdfs:
        invoice_id = sha256_file(pdf)
        existing = store.find_by_id(invoice_id)
        if existing is not None:
            if existing.fakturoid.status == "imported":
                reason = f"already imported (expense_id={existing.fakturoid.expense_id})"
            else:
                reason = "already extracted"
            console.print(f"[yellow]skip[/yellow] {pdf.name} — {reason}")
            continue
        console.print(f"[cyan]extract[/cyan] {pdf.name}")
        rendered = render_pdf(pdf)
        extracted = extractor.extract(rendered)
        record = ExportRecord.from_extraction(
            invoice_id=invoice_id,
            source_pdf=str(pdf.resolve()),
            extracted_at=datetime.now(UTC),
            extracted=extracted,
        )
        store.upsert(record)

    console.print(f"[green]wrote[/green] {output}")


@app.command("import")
def import_cmd(
    export_path: Path = typer.Argument(
        ..., exists=True, file_okay=False, dir_okay=True, help="Directory of invoice sidecars."
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    auto_create_subjects: bool = typer.Option(False, "--auto-create-subjects"),
    no_create: bool = typer.Option(False, "--no-create"),
    refresh_subjects: bool = typer.Option(False, "--refresh-subjects"),
) -> None:
    """Import pending invoices from the sidecar directory into Fakturoid."""
    if auto_create_subjects and no_create:
        err_console.print(
            "[red]--auto-create-subjects and --no-create are mutually exclusive[/red]"
        )
        raise typer.Exit(code=2)

    store = ExportStore(export_path)
    records = store.records()
    if not records:
        console.print("Nothing to import.")
        return

    client, http = _build_fakturoid()
    try:
        subjects = SubjectStore(client=client)
        runner = ImportRunner(
            client=client,
            subjects=subjects,
            pdf_root=export_path,
            vendor_prompt=_vendor_prompt,
        )
        flags = ImportFlags(
            dry_run=dry_run,
            auto_create_subjects=auto_create_subjects,
            no_create=no_create,
            refresh_subjects=refresh_subjects,
        )

        for record in records:
            if record.fakturoid.status == "imported":
                console.print(f"[yellow]skip[/yellow] {record.invoice_number} — already imported")
                continue
            try:
                outcome = runner.run_one(record, flags)
            except (AlreadyImportedError, VendorNotFoundError) as e:
                err_console.print(f"[red]{record.invoice_number}:[/red] {e}")
                store.update_status(record.id, status="error", error=str(e))
                raise typer.Exit(code=1) from e
            except Exception as e:
                store.update_status(record.id, status="error", error=str(e))
                raise

            store.update_status(
                record.id,
                status=outcome.status,
                subject_id=outcome.subject_id,
                expense_id=outcome.expense_id,
                imported_at=outcome.imported_at,
            )
            console.print(
                f"[green]{outcome.status}[/green] {record.invoice_number}"
                + (f" (expense_id={outcome.expense_id})" if outcome.expense_id else "")
            )
    finally:
        http.close()


@app.command()
def status(
    export_path: Path = typer.Argument(
        ..., exists=True, file_okay=False, dir_okay=True, help="Directory of invoice sidecars."
    ),
) -> None:
    """Show a status table for the invoices in the sidecar directory."""
    store = ExportStore(export_path)
    table = Table(title=f"faktspense — {export_path}")
    table.add_column("Invoice")
    table.add_column("Vendor")
    table.add_column("IČO")
    table.add_column("Total")
    table.add_column("Status")
    table.add_column("Expense ID")
    for rec in store.records():
        table.add_row(
            rec.invoice_number,
            rec.vendor.name,
            rec.vendor.ico or "—",
            str(rec.total) if rec.total is not None else "—",
            rec.fakturoid.status,
            str(rec.fakturoid.expense_id) if rec.fakturoid.expense_id else "—",
        )
    console.print(table)


def main() -> None:  # pragma: no cover - thin wrapper
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
