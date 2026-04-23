# faktspense

CLI tool that extracts invoice data from PDF files and imports them as expenses into [Fakturoid.cz](https://www.fakturoid.cz/) via the v3 API.

## How it works

```
extract invoice.pdf  →  review/edit export.json  →  import export.json
```

1. **Extract** — renders PDF pages, sends to Claude API, writes structured data to `export.json`
2. **Review** — open `export.json` in any editor, fix any extraction errors
3. **Import** — reads `export.json`, matches vendors by IČO, creates expenses in Fakturoid with the original PDF attached

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Fakturoid account with OAuth2 app (Client Credentials)
- Anthropic API key

## Setup

```bash
git clone https://github.com/Belgor/faktspense.git
cd faktspense
uv sync
```

Set environment variables:

```bash
export FAKTUROID_CLIENT_ID=...
export FAKTUROID_CLIENT_SECRET=...
export FAKTUROID_SLUG=...          # your Fakturoid account slug
export ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

```bash
# Extract one PDF
faktspense extract invoice.pdf

# Extract all PDFs in a directory
faktspense extract invoices/

# Review and edit export.json, then import
faktspense import export.json

# Preview without writing to Fakturoid
faktspense import export.json --dry-run

# Skip vendor confirmation prompts (bulk import)
faktspense import export.json --auto-create-subjects

# Fail if vendor not found (strict mode)
faktspense import export.json --no-create

# Show import status
faktspense status export.json
```

## Vendor matching

When a vendor IČO is not found among existing Fakturoid subjects, the tool pauses and asks:

```
⚠  Vendor not found in Fakturoid
   Extracted:  ACME s.r.o.   IČO: 12345678

   [1] Create new subject from extracted data
   [2] Map to existing subject
   [3] Skip this invoice
```

Use `--auto-create-subjects` to skip this prompt in batch workflows.

## Duplicate protection

Each invoice is tracked in `export.json` with its Fakturoid `expense_id` and `imported_at` timestamp. Re-running import on an already-imported invoice raises an error rather than creating a duplicate.
