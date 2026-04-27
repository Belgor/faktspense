# Manual smoke test

End-to-end CLI walkthrough that requires real credentials, real PDFs, and
network access. Not part of CI — run once before tagging a release, or any
time you want to confirm the human-facing flow still works.

For an automated equivalent that runs the same flow on every PDF in
`test_data_real/` and emits a structured report, see
[AUTOTEST.md](AUTOTEST.md). The two are complementary: this checklist
exercises the interactive vendor prompt and the CLI ergonomics; the
autotest exercises the API wiring and produces machine-readable
artifacts.

## Prereqs

Production env vars (the CLI reads these — note: **no `TEST_` prefix**;
the autotest uses the prefixed variant):

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

2. **Extract → sidecar directory**
   ```bash
   uv run faktspense extract invoice.pdf -o ./export/
   ```
   Writes `./export/<safe_pdf_stem>_<sha8>.json`. The `<sha8>` is the
   first 8 chars of the PDF's sha256; the full sha256 lives in the
   sidecar's `id` field and is the Fakturoid `custom_id`.

   Open the sidecar and verify:
   - `vendor.name`, `vendor.ico` (8 digits, leading zeros preserved),
     `vendor.dic`, `vendor.address`
   - `invoice_number`, `issued_on`, `due_on`, `taxable_fulfillment_due`
   - `lines[]` (name, quantity, unit_price, vat_rate)
   - `total`

   Edit by hand if anything is wrong. The `fakturoid` block should show
   `status: "pending"` with all other fields `null`.

3. **Status (pre-import)**
   ```bash
   uv run faktspense status ./export/
   ```
   All rows should show `pending`.

4. **Dry-run the import** — payload shape only, no Fakturoid writes.
   ```bash
   uv run faktspense import ./export/ --dry-run
   ```

5. **Live import**
   ```bash
   uv run faktspense import ./export/
   ```
   If the vendor's IČO is unknown to Fakturoid, the interactive prompt
   lists candidates from the cached subject list — pick **create**, **map**,
   or **skip**. Use `--auto-create-subjects` to skip the prompt and
   silently create new vendors, or `--no-create` to fail on missing
   vendors instead.

   On success, each sidecar is rewritten in place with `subject_id`,
   `expense_id`, `imported_at`, and `status: "imported"`. Writes are
   atomic per record, so a partial batch is safe to resume.

6. **Status (post-import)**
   ```bash
   uv run faktspense status ./export/
   ```
   Every row should show `imported` with an `expense_id`.

7. **Idempotency** — re-running import must refuse to double-post.
   ```bash
   uv run faktspense import ./export/   # expect "already imported" per row
   ```
   Confirm zero new POSTs in the Fakturoid UI.

8. **Verify in Fakturoid UI**:
   - Expense present under the expected supplier.
   - Original PDF attached.
   - `custom_id` matches the sidecar's `id` (full sha256 hex).
   - `original_number` matches the vendor's invoice number from the sidecar (not `number`, which is Fakturoid's own sequence).
   - Subject IČO matches `vendor.ico` from the sidecar (8-digit, zero-padded).

## Subject cache

After the first import, a local cache exists at
`~/.cache/faktspense/subjects_<slug>.json`. Force a refresh with
`--refresh-subjects` if Fakturoid subjects were changed externally —
otherwise the cache is consulted first and only a stale cache miss
triggers a re-fetch.

## Re-extract / change detection

If the source PDF changes, re-running `extract` on the same input
detects the new sha256, re-extracts, and `ExportStore.upsert` deletes
the stale sidecar. Each PDF maps to at most one current sidecar.

If the sidecar is already at the imported state and you re-extract a
modified PDF, the new sidecar starts fresh (`status: "pending"`); the
already-imported expense in Fakturoid is **not** automatically updated.
That's a manual cleanup.

## Rate limits

Fakturoid's public rate limit is 100 requests/hour. For a small batch
this is not a concern. On 429 the client sleeps for `Retry-After` and
retries once. Check logs at DEBUG level (the client logs
`X-RateLimit-Remaining` after every request) if a run looks slow.
