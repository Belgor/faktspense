# faktspense

CLI tool that extracts invoice data from PDF files and imports them as expenses into [Fakturoid.cz](https://www.fakturoid.cz/) via the v3 API.

## How it works

```
extract invoice.pdf  →  review/edit export.json  →  import export.json
```

1. **Extract** — renders PDF pages, sends them to the Claude API, writes structured data to `export.json`.
2. **Review** — open `export.json` in any editor and fix any extraction errors.
3. **Import** — matches vendors by IČO, creates expenses in Fakturoid with the original PDF attached, and tracks state back into `export.json`.

Each invoice is keyed by `sha256(pdf_bytes)`, used as the Fakturoid `custom_id` for idempotency. Running import twice on the same PDF refuses to double-post.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- A Fakturoid account with an **OAuth2 app** (Client Credentials grant) — see *Fakturoid setup* below
- An Anthropic API key

## Install

```bash
git clone https://github.com/Belgor/faktspense.git
cd faktspense
uv sync
```

## Configuration

All configuration is via environment variables. The tool does **not** load `.env` files.

```bash
export FAKTUROID_CLIENT_ID=...
export FAKTUROID_CLIENT_SECRET=...
export FAKTUROID_SLUG=...            # your Fakturoid account slug (from the URL)
export ANTHROPIC_API_KEY=sk-ant-...

# Optional: override the extraction model (default: claude-haiku-4-5)
export ANTHROPIC_MODEL=claude-haiku-4-5
```

### Fakturoid setup

1. Sign in to Fakturoid and open **Nastavení → Vývojářské → API aplikace**.
2. Create a new app with the **Client Credentials** grant type.
3. Copy the Client ID and Client Secret into the env vars above.
4. The account `slug` is the subdomain in `https://app.fakturoid.cz/<slug>/`.

### Anthropic setup

Create an API key at https://console.anthropic.com/ and export it as `ANTHROPIC_API_KEY`.

## Usage

```bash
# Extract one PDF (writes ./export.json by default)
faktspense extract invoice.pdf

# Extract every *.pdf in a directory
faktspense extract invoices/

# Custom output path
faktspense extract invoice.pdf --output my-batch.json

# Review: open export.json in any editor, fix any extraction errors.

# Preview the payload without writing to Fakturoid
faktspense import export.json --dry-run

# Import for real
faktspense import export.json

# Skip the vendor-confirmation prompt (bulk mode — creates missing subjects silently)
faktspense import export.json --auto-create-subjects

# Strict mode — fail if a vendor doesn't exist
faktspense import export.json --no-create

# Force a refresh of the local Fakturoid subjects cache
faktspense import export.json --refresh-subjects

# Status table
faktspense status export.json
```

## Vendor matching

When a vendor IČO isn't found among existing Fakturoid subjects, the tool pauses and asks:

```
⚠  Vendor not found in Fakturoid
   Extracted:  ACME s.r.o.   IČO: 12345678

   [1] Create new subject from extracted data  (default)
   [2] Map to existing: 'Acme Czech s.r.o.' (IČO: 12345678)
   [3] Skip this invoice
```

- `--auto-create-subjects` silently creates new subjects (useful for batch runs).
- `--no-create` turns missing vendors into errors (useful for strict reconciliation flows).

## Subject cache

Fakturoid's subjects (vendors) are fetched once and cached to `~/.cache/faktspense/subjects_<slug>.json`. This keeps the tool usable under Fakturoid's 100 req/hour rate limit.

Use `--refresh-subjects` to force a full re-fetch after vendors were added or edited outside the tool.

## Duplicate protection

Each invoice is tracked in `export.json` with its Fakturoid `expense_id`, `imported_at`, and `status` (`pending` | `imported` | `error` | `skipped`). Re-running import on an already-imported invoice raises an error rather than creating a duplicate. Writes are atomic and flushed after each record, so killing the process mid-batch leaves `export.json` consistent.

## Development

```bash
uv sync
uv run pytest                                         # 51 tests, ~1s
uv run pytest --cov=fakturoid_naklady -q              # ~89% line coverage
uv run ruff check . && uv run ruff format --check .
```

- Architecture and contributor notes live in [`CLAUDE.md`](./CLAUDE.md).
- Before tagging a release, run through [`docs/SMOKE.md`](./docs/SMOKE.md) (requires real credentials).

## Troubleshooting

**`400 Bad Request` on every Fakturoid call.** Fakturoid rejects requests without a `User-Agent`. The tool always sends `User-Agent: faktspense/0.1`; if you're customizing the client, keep that header.

**`401 Unauthorized`.** Tokens expire after ~2 hours; the client re-fetches automatically on 401. If it keeps happening, re-check `FAKTUROID_CLIENT_ID` / `FAKTUROID_CLIENT_SECRET` and the OAuth app's grant type (must be Client Credentials).

**`429 Too Many Requests`.** Fakturoid limits to 100 requests/hour. The client respects `Retry-After` and retries once; for large batches, use `--auto-create-subjects` and/or enable the subject cache (it's on by default — one cache fill instead of N lookups).

**Extraction got the vendor wrong.** Open `export.json` in an editor, fix the fields, and run `import`. The extracted values are inputs to `import`, not a fixed record — editing them is expected workflow.

**`already imported` error on re-import.** This is by design — it prevents duplicate expenses. To retry a specific record, edit its `fakturoid.status` back to `"pending"` in `export.json`.

## License

MIT
