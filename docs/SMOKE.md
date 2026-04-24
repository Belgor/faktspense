# Manual smoke test

End-to-end checks that require real credentials and network access.
Not part of CI — run once before tagging a release.

## Prereqs

```bash
export FAKTUROID_CLIENT_ID=...
export FAKTUROID_CLIENT_SECRET=...
export FAKTUROID_SLUG=...
export ANTHROPIC_API_KEY=sk-ant-...
```

A real PDF invoice at `./invoice.pdf` (any Czech supplier invoice).

## Steps

1. **Install**
   ```bash
   uv sync
   ```

2. **Extract**
   ```bash
   uv run faktspense extract invoice.pdf
   python -m json.tool export.json | less
   ```
   Verify the vendor name, IČO (8 digits, leading zeros preserved), and line items look correct.
   Edit `export.json` by hand if anything is wrong.

3. **Dry-run the import** — payload shape only, no Fakturoid writes.
   ```bash
   uv run faktspense import export.json --dry-run
   ```

4. **Live import**
   ```bash
   uv run faktspense import export.json
   ```
   If the vendor is new, the interactive prompt lists candidates; pick create/map/skip.
   On success, `export.json` is updated with `expense_id`, `imported_at`, `status=imported`.

5. **Status**
   ```bash
   uv run faktspense status export.json
   ```

6. **Idempotency check** — re-running import must refuse to double-post.
   ```bash
   uv run faktspense import export.json   # expect ERROR: already imported
   ```

7. **Verify in Fakturoid UI**:
   - Expense present under the expected supplier.
   - Original PDF attached.
   - `custom_id` matches the record `id` (sha256 hex) in `export.json`.

## Subject cache

After the first import, a local cache exists at `~/.cache/faktspense/subjects_<slug>.json`.
Force a refresh with `--refresh-subjects` if Fakturoid subjects were changed externally.

## Rate limits

Fakturoid's public rate limit is 100 requests/hour. For a small batch this is not a concern.
On 429 the client sleeps for `Retry-After` and retries once. Check logs if a run looks slow.
