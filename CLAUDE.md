# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**faktspense** — CLI tool that extracts structured invoice data from PDF files using the Claude API and imports them as expenses into [Fakturoid.cz](https://www.fakturoid.cz/) via the v3 REST API. Not a general-purpose Fakturoid client — scope is limited to expense (náklady) import.

Two-step flow: `extract` → user reviews/edits per-invoice JSON sidecars in the output dir → `import`.

## Stack

- **Python 3.12+**, package manager: **uv**
- `pymupdf` — PDF → PNG rendering + text extraction
- `anthropic` — Claude API (`claude-haiku-4-5` by default, override via `ANTHROPIC_MODEL`)
- `httpx` — Fakturoid HTTP client
- `pydantic >= 2.0` — data models + validation
- `typer` — CLI
- `rich` — tables, interactive vendor-match prompt
- Tests: `pytest`, `pytest-httpx`, `syrupy`, `ruff`

## Commands

```bash
# Install
uv sync

# Extract PDFs → ./export/<pdf_stem>_<sha8>.json (one sidecar per PDF)
uv run faktspense extract invoice.pdf
uv run faktspense extract invoices/                   # whole directory
uv run faktspense extract invoice.pdf -o ./batch/

# Import from sidecar dir → Fakturoid
uv run faktspense import ./export/
uv run faktspense import ./export/ --dry-run
uv run faktspense import ./export/ --auto-create-subjects
uv run faktspense import ./export/ --no-create
uv run faktspense import ./export/ --refresh-subjects

# Status
uv run faktspense status ./export/

# Tests + coverage
uv run pytest
uv run pytest --cov=fakturoid_naklady --cov-report=term-missing

# Lint + format
uv run ruff check .
uv run ruff format --check .
```

## Environment variables (required, no .env loading)

```bash
FAKTUROID_CLIENT_ID=...
FAKTUROID_CLIENT_SECRET=...
FAKTUROID_SLUG=...                    # Fakturoid account slug
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-haiku-4-5      # optional override
```

## Architecture

```
src/fakturoid_naklady/
├── models.py              # Pydantic: VendorInfo, InvoiceLine, ExtractedInvoice,
│                          #           FakturoidStatus, ExportRecord,
│                          #           FakturoidStatusValue Literal
├── export.py              # ExportStore: directory of <pdf_stem>_<sha8>.json sidecars,
│                          #   atomic per-record writes, upsert / find_by_id /
│                          #   update_status / records / path_for
├── fakturoid/
│   ├── auth.py            # TokenProvider protocol + OAuth2TokenProvider + StaticTokenProvider
│   ├── client.py          # FakturoidClient: UA, auth header, 401 refetch, 429 backoff, FakturoidError
│   ├── subjects.py        # SubjectStore: paginated fetch-all + disk cache + IČO + fuzzy match + create
│   └── expenses.py        # build_expense_payload (pure) + create_expense (I/O)
├── extraction/
│   ├── renderer.py        # PyMuPDF: PDF → list[PNG bytes] + text layer (RenderedPdf)
│   └── claude.py          # ClaudeExtractor: RenderedPdf → ExtractedInvoice + one retry
├── pipeline.py            # ImportRunner.run_one(record, flags); ImportFlags, ImportOutcome,
│                          #   VendorPrompt protocol, VendorPromptAction Literal, exceptions
└── cli.py                 # typer: extract / import / status commands
```

**Dependency-injection seams** — every adapter is injected so tests swap real clients for doubles:

- `FakturoidClient(http=..., token_provider=...)` — swap in `pytest-httpx` transport + `StaticTokenProvider`
- `ClaudeExtractor(client=...)` — swap in `tests.conftest.StubAnthropic`
- `SubjectStore(cache_path=...)` — tests point cache at `tmp_path`
- `ImportRunner(vendor_prompt=..., now=...)` — prompt and clock injectable

**Pure vs. impure boundary:** models, `export` helpers, `build_expense_payload`, and hashing are pure — unit-tested without I/O. Network/filesystem sits at the edges.

## Sidecar directory — review artifact + state tracker

The output of `extract` is a directory; each PDF gets its own JSON file
(`ExportStore` in `export.py`). One sidecar per invoice — extracted fields
(editable) plus a `fakturoid` block:

```json
{
  "fakturoid": {
    "subject_id": null,
    "expense_id": null,
    "imported_at": null,
    "status": "pending",
    "error": null
  }
}
```

`status` values (`FakturoidStatusValue`): `pending` | `imported` | `error` | `skipped`

**Filename rule:** `<safe_pdf_stem>_<sha8>.json`. `safe_pdf_stem` is the
PDF filename without extension, with anything outside `[A-Za-z0-9._-]`
replaced by `_`. `sha8` is the first 8 chars of the full sha256 of the
PDF bytes. The full sha256 lives in the sidecar's `id` field — it is the
Fakturoid `custom_id` and the source-of-truth for change detection.

**Re-run / change detection.** On every `extract` pass the tool computes
each input PDF's sha256. If a sidecar already has that exact `id` →
skipped. If not → re-extracts and `ExportStore.upsert` deletes any stale
sidecar that pointed at the same `source_pdf` with a different hash, so
each PDF maps to at most one current sidecar.

Writes are atomic (temp file + `os.replace`); each sidecar is rewritten
immediately on every state change so a partial batch is always safe to
resume.

## Hard rules

- **Never** load `.env` files — credentials come from env vars only.
- **Always** send `User-Agent: faktspense/0.1` on every Fakturoid request. Omitting it returns 400. Do not embed email or other PII in the header (see memory: no-PII-in-User-Agent feedback).
- **Never** POST to Fakturoid if `fakturoid.status == "imported"` — `ImportRunner.run_one` raises `AlreadyImportedError` with existing `expense_id` and `imported_at`.
- **Always** use `original_number` (not `number`) for the vendor's invoice number. The `number` field is Fakturoid's own sequence number and must match the account's configured format — omit it so Fakturoid auto-assigns.
- **Always** attach the original PDF as base64 `data:application/pdf;base64,...` in the `attachments` array on every expense.
- **Never** import without setting `custom_id = record.id` — this is the idempotency key.
- **Always** preserve IČO as 8-char string (validator in `VendorInfo` zero-pads; never coerce to int).

## Vendor matching flow (`pipeline.ImportRunner._resolve_subject`)

1. Extract IČO from invoice.
2. `SubjectStore.find_by_ico` — cache lookup, fallback to live re-fetch if cache was stale.
3. If not found, apply flags in order:
   - `--auto-create-subjects` → silently `SubjectStore.create(vendor)`
   - `--no-create` → raise `VendorNotFoundError`
   - otherwise → call the `VendorPrompt` callable (Rich interactive prompt in the CLI), which returns `(VendorPromptAction, dict | None)` where action ∈ `"create" | "map" | "skip"`.

## Subject cache

- Path: `~/.cache/faktspense/subjects_{slug}.json` (override via `SubjectStore(cache_path=...)`; default comes from `fakturoid.subjects.default_cache_path(slug)`).
- Populated by paginated `GET /subjects.json?page=N` (stop-on-empty — do not assume a page size).
- `--refresh-subjects` forces a full re-fetch.
- On IČO lookup miss with a disk-cached store, automatically re-fetches once (cache may be stale). On miss with a fresh fetch, returns `None`.

## Testing conventions

- **Shared fixtures** live in `tests/conftest.py`: `http_client`, `fakturoid_client` (slug=acme, `StaticTokenProvider("tkn")`), `sample_pdf` (one-page PDF with Czech text), `subjects_cache` (factory), `patched_default_cache_path` (monkeypatches the default cache path), and the `StubAnthropic` test double.
- Use `pytest-httpx` for HTTP-boundary tests. When the code calls `_fetch_all`, register **two** page responses (the data page plus an empty terminator) because pagination stops on empty response.
- No test makes a real Fakturoid or Anthropic API call. Live verification is a manual checklist in `docs/SMOKE.md`.
- Coverage gate: ≥ 85 % on `src/fakturoid_naklady/` (`cli.py`'s typer wiring is the main exception).
- New tests go under `tests/unit/` (pure), `tests/integration/` (HTTP boundary), or `tests/e2e/` (CLI with all externals stubbed).

## Fakturoid API v3 endpoints used

| Operation | Endpoint |
|---|---|
| Token | `POST https://app.fakturoid.cz/api/v3/oauth/token` (Client Credentials) |
| List subjects | `GET /accounts/{slug}/subjects.json?page=N` (paginated, stop-on-empty) |
| Create subject | `POST /accounts/{slug}/subjects.json` |
| Create expense | `POST /accounts/{slug}/expenses.json` |

Base URL: `https://app.fakturoid.cz/api/v3`
Token expires: 2 hours. Re-fetched automatically on 401 (in-memory cache, no disk persistence).
Rate limit: 100 req/hour. `X-RateLimit-Remaining` is logged at DEBUG; 429 triggers a single `Retry-After` sleep + retry.

## References

- Fakturoid API v3: https://www.fakturoid.cz/api/v3
- Expenses endpoint: https://www.fakturoid.cz/api/v3/expenses
- Auth: https://www.fakturoid.cz/api/v3/authorization
- Manual smoke checklist: `docs/SMOKE.md`
