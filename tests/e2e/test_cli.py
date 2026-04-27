from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from fakturoid_naklady import cli as cli_mod
from fakturoid_naklady.cli import app
from fakturoid_naklady.export import ExportStore
from fakturoid_naklady.extraction.claude import ClaudeExtractor
from tests.conftest import StubAnthropic

_VALID_EXTRACTION = {
    "vendor": {"name": "ACME s.r.o.", "ico": "12345678", "dic": "CZ12345678"},
    "invoice_number": "2024-0042",
    "issued_on": "2024-03-15",
    "due_date": "2024-03-29",
    "currency": "CZK",
    "lines": [
        {"name": "Work", "quantity": "1", "unit_name": "h", "unit_price": "1000", "vat_rate": 21}
    ],
    "total": 1210,
    "total_vat": 210,
}


@pytest.fixture
def fakturoid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKTUROID_CLIENT_ID", "cid")
    monkeypatch.setenv("FAKTUROID_CLIENT_SECRET", "sec")
    monkeypatch.setenv("FAKTUROID_SLUG", "acme")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")


@pytest.fixture
def stub_extractor(monkeypatch: pytest.MonkeyPatch) -> None:
    def _build() -> ClaudeExtractor:
        return ClaudeExtractor(
            client=StubAnthropic(json.dumps(_VALID_EXTRACTION)),
            model="claude-haiku-4-5",
        )

    monkeypatch.setattr(cli_mod, "_build_extractor", _build)


def _expected_sidecar(out_dir: Path, pdf: Path) -> Path:
    sha8 = hashlib.sha256(pdf.read_bytes()).hexdigest()[:8]
    return out_dir / f"{pdf.stem}_{sha8}.json"


def test_extract_writes_per_invoice_sidecar(
    tmp_path: Path, sample_pdf: Path, fakturoid_env: None, stub_extractor: None
) -> None:
    runner = CliRunner()
    out = tmp_path / "out"
    result = runner.invoke(app, ["extract", str(sample_pdf), "--output", str(out)])
    assert result.exit_code == 0, result.output

    sidecar = _expected_sidecar(out, sample_pdf)
    assert sidecar.exists(), list(out.iterdir())
    rec = json.loads(sidecar.read_text())
    assert rec["invoice_number"] == "2024-0042"
    assert rec["fakturoid"]["status"] == "pending"
    assert len(rec["id"]) == 64  # full sha256 retained inside


def test_extract_is_idempotent(
    tmp_path: Path, sample_pdf: Path, fakturoid_env: None, stub_extractor: None
) -> None:
    runner = CliRunner()
    out = tmp_path / "out"
    runner.invoke(app, ["extract", str(sample_pdf), "--output", str(out)])
    runner.invoke(app, ["extract", str(sample_pdf), "--output", str(out)])
    files = sorted(p.name for p in out.glob("*.json"))
    assert len(files) == 1


def test_import_dry_run_does_not_post(
    tmp_path: Path,
    sample_pdf: Path,
    fakturoid_env: None,
    stub_extractor: None,
    httpx_mock: HTTPXMock,
    patched_default_cache_path: Path,
) -> None:
    runner = CliRunner()
    out = tmp_path / "out"
    runner.invoke(app, ["extract", str(sample_pdf), "--output", str(out)])

    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/oauth/token",
        json={"access_token": "t", "token_type": "Bearer", "expires_in": 7200},
    )
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/subjects.json?page=1",
        json=[{"id": 7, "name": "ACME s.r.o.", "registration_no": "12345678"}],
    )
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/subjects.json?page=2",
        json=[],
    )

    result = runner.invoke(app, ["import", str(out), "--dry-run"])
    assert result.exit_code == 0, result.output

    rec = json.loads(_expected_sidecar(out, sample_pdf).read_text())
    assert rec["fakturoid"]["status"] == "pending"
    assert rec["fakturoid"]["expense_id"] is None
    assert rec["fakturoid"]["subject_id"] == 7
    posts = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    assert all("/expenses.json" not in str(r.url) for r in posts)


def test_import_live_then_blocks_reimport(
    tmp_path: Path,
    sample_pdf: Path,
    fakturoid_env: None,
    stub_extractor: None,
    httpx_mock: HTTPXMock,
    patched_default_cache_path: Path,
) -> None:
    runner = CliRunner()
    out = tmp_path / "out"
    runner.invoke(app, ["extract", str(sample_pdf), "--output", str(out)])

    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/oauth/token",
        json={"access_token": "t", "token_type": "Bearer", "expires_in": 7200},
        is_reusable=True,
    )
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/subjects.json?page=1",
        json=[{"id": 7, "name": "ACME s.r.o.", "registration_no": "12345678"}],
    )
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/subjects.json?page=2",
        json=[],
    )
    httpx_mock.add_response(
        url="https://app.fakturoid.cz/api/v3/accounts/acme/expenses.json",
        method="POST",
        json={"id": 999, "number": "2024-0042"},
    )

    result = runner.invoke(app, ["import", str(out)])
    assert result.exit_code == 0, result.output
    rec = json.loads(_expected_sidecar(out, sample_pdf).read_text())
    assert rec["fakturoid"]["status"] == "imported"
    assert rec["fakturoid"]["expense_id"] == 999

    before = len(httpx_mock.get_requests(method="POST"))
    result2 = runner.invoke(app, ["import", str(out)])
    assert result2.exit_code == 0
    after = len(httpx_mock.get_requests(method="POST"))
    assert after == before


def test_status_command_runs(
    tmp_path: Path, sample_pdf: Path, fakturoid_env: None, stub_extractor: None
) -> None:
    runner = CliRunner()
    out = tmp_path / "out"
    runner.invoke(app, ["extract", str(sample_pdf), "--output", str(out)])
    result = runner.invoke(app, ["status", str(out)])
    assert result.exit_code == 0
    assert "2024-0042" in result.output


def test_extract_re_extracts_when_pdf_content_changes(
    tmp_path: Path, sample_pdf: Path, fakturoid_env: None, stub_extractor: None
) -> None:
    runner = CliRunner()
    out = tmp_path / "out"
    runner.invoke(app, ["extract", str(sample_pdf), "--output", str(out)])

    store = ExportStore(out)
    [first] = store.records()
    old_path = store.path_for(first)
    assert old_path.exists()

    # Mutate the PDF bytes — appending changes its sha256 but most viewers
    # still treat it as a valid PDF (the trailer comes before the appended bytes).
    sample_pdf.write_bytes(sample_pdf.read_bytes() + b"\n%% touched\n")

    runner.invoke(app, ["extract", str(sample_pdf), "--output", str(out)])

    files = sorted(p.name for p in out.glob("*.json"))
    assert len(files) == 1
    assert not old_path.exists()  # stale sidecar removed
    [updated] = ExportStore(out).records()
    assert updated.id != first.id
