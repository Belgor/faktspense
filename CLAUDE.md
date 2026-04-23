# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**faktspense** — CLI tool that extracts structured invoice data from PDF files using the Claude API and imports them as expenses into [Fakturoid.cz](https://www.fakturoid.cz/) via the v3 REST API. Not a general-purpose Fakturoid client — scope is limited to expense (náklady) import.

Two-step flow: `extract` → user reviews/edits `export.json` → `import`.

## Stack

- **Python 3.12**, package manager: **uv**
- `pymupdf` — PDF → PNG rendering + text extraction
- `anthropic` — Claude API (`claude-haiku-4-5` by default, override via `ANTHROPIC_MODEL`)
- `httpx` — Fakturoid HTTP client
- `pydantic >= 2.0` — data models + validation
- `typer` — CLI
- `rich` — tables, interactive vendor-match prompt

## Commands

```bash
# Install
uv sync

# Extract PDFs → export.json
uv run faktspense extract invoice.pdf
uv run faktspense extract invoices/              # whole directory

# Import from export.json → Fakturoid
uv run faktspense import export.json
uv run faktspense import export.json --dry-run
uv run faktspense import export.json --auto-create-subjects
uv run faktspense import export.json --no-create

# Show status table
uv run faktspense status export.json

# Test
uv run pytest

# Lint
uv run ruff check . && uv run ruff format --check .
```

## Environment variables (required, no .env loading)

```bash
FAKTUROID_CLIENT_ID=...
FAKTUROID_CLIENT_SECRET=...
FAKTUROID_SLUG=...            # Fakturoid account slug
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-haiku-4-5   # optional override
```

## Architecture

```
src/fakturoid_naklady/
├── models.py          # Pydantic: VendorInfo, InvoiceLine, ExtractedInvoice,
│                      #           FakturoidStatus, ExportRecord, ExportFile
├── export.py          # read/write/merge export.json; find-by-id; update-status
├── fakturoid/
│   ├── auth.py        # OAuth2 Client Credentials — fetch + in-memory cache
│   ├── client.py      # httpx base — auth header, User-Agent, 429 backoff
│   ├── subjects.py    # lookup by IČO, fuzzy name match (difflib), create
│   └── expenses.py    # create expense + base64 PDF attachment
├── extraction/
│   ├── renderer.py    # PyMuPDF: PDF → list[PNG bytes] + optional text
│   └── claude.py      # images → ExtractedInvoice via Claude API
├── pipeline.py        # ImportPipeline.run(record, flags) — orchestrates all
└── cli.py             # typer: extract / import / status
```

## export.json — review artifact + state tracker

Single file per batch. Each invoice entry has extracted fields (editable) plus a `fakturoid` block:

```json
{
  "fakturoid": {
    "subject_id": null,
    "expense_id": null,
    "imported_at": null,
    "status": "pending"
  }
}
```

`status` values: `pending` | `imported` | `error` | `skipped`

The `id` field is `sha256(pdf_bytes)` — used as both dedup key and Fakturoid `custom_id`.

## Hard rules

- **Never** load .env files — credentials come from env vars only.
- **Always** include `User-Agent: fakturoid-naklady/0.1 (ai.claude@brehovsky.cz)` on every Fakturoid request. Omitting it returns 400.
- **Never** POST to Fakturoid if `fakturoid.status == "imported"` — error out with expense_id and imported_at.
- **Always** attach the original PDF as base64 `data:application/pdf;base64,...` in the `attachments` array when creating the expense.
- **Never** import without setting `custom_id` (= record `id`) — this is the idempotency key.

## Vendor matching flow

1. Extract IČO from invoice
2. Search Fakturoid Subjects by IČO
3. If not found → **raise interactive Rich prompt** (show extracted vendor + fuzzy name matches)
   - `--auto-create-subjects`: skip prompt, silently create subject
   - `--no-create`: skip prompt, exit with error

## Fakturoid API v3 endpoints used

| Operation | Endpoint |
|---|---|
| Token | `POST https://app.fakturoid.cz/oauth/token` (Client Credentials) |
| List subjects | `GET /accounts/{slug}/subjects.json` |
| Create subject | `POST /accounts/{slug}/subjects.json` |
| Create expense | `POST /accounts/{slug}/expenses.json` |

Base URL: `https://app.fakturoid.cz/api/v3`
Token expires: 2 hours (re-fetch automatically on 401, no disk caching needed for CLI).
Rate limit: 100 req/hour — check `X-RateLimit` headers.

## References

- Fakturoid API v3: https://www.fakturoid.cz/api/v3
- Expenses endpoint: https://www.fakturoid.cz/api/v3/expenses
- Auth: https://www.fakturoid.cz/api/v3/authorization
