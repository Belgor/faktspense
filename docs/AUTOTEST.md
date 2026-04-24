# End-to-end autotest against a real Fakturoid test account

`scripts/e2e_real.py` runs the full `extract → import → validate → idempotency`
flow on every PDF in `test_data_real/` against a real Fakturoid account using
real Anthropic API calls.

Not wired into pytest — `uv run pytest` never touches real APIs. Run this
manually before tagging a release, or whenever you want to confirm the full
pipeline against live services.

## Prerequisites

A **dedicated Fakturoid test account** (Fakturoid has no sandbox — use a
separate real account you don't mind filling with test data).

Credentials are provided as **`sbx` sandbox secrets**, not plain
environment exports. The script is designed to run inside the sandbox
where `sbx` injects them automatically as `TEST_`-prefixed env vars.
The prefix is deliberate: it keeps test-account credentials lexically
distinct from the unprefixed production credentials the CLI reads, so
neither can be invoked with the other's keys.

Set them once on the host:

```bash
sbx secret set <sandbox-name> TEST_FAKTUROID_CLIENT_ID     -t "..."
sbx secret set <sandbox-name> TEST_FAKTUROID_CLIENT_SECRET -t "..."
sbx secret set <sandbox-name> TEST_FAKTUROID_SLUG          -t "..."
sbx secret set <sandbox-name> TEST_ANTHROPIC_API_KEY       -t "sk-ant-..."

# Verify:
sbx secret list
```

Secrets are injected at sandbox startup. After adding or rotating a
secret, restart the sandbox so the new values reach the script's
environment — an already-running session will not see them.

Input PDFs live in `test_data_real/` (gitignored).

## Run

```bash
# Full run, artifacts left behind in the test account for inspection:
uv run python scripts/e2e_real.py

# Full run + delete everything the script created at the end:
uv run python scripts/e2e_real.py --cleanup

# Resume from a partial run (uses test_data_real/_run/export.json as-is):
uv run python scripts/e2e_real.py --skip-extract

# Validate an existing import without importing again:
uv run python scripts/e2e_real.py --skip-extract --skip-import
```

Scratch files land in `test_data_real/_run/`:
- `export.json` — the usual review artifact, plus Fakturoid state per record
- `subjects_cache.json` — isolated subject cache (does not touch `~/.cache/faktspense/`)

## What the script asserts

**Hard failures** (exit code 1 if any):

| Phase        | Assertion |
|--------------|-----------|
| extract      | Claude returns valid `ExtractedInvoice` for every PDF |
| import       | Every record reaches `status=imported` with a subject + expense id |
| validate     | Fetched expense has matching `custom_id`, `number`, `subject_id`, and at least one attachment |
| idempotency  | Re-running `import` on each record raises `AlreadyImportedError` (no second POST) |

**Soft diffs** (reported but non-fatal — they flag extraction-quality drift):

- `total` differs between what Claude extracted and what Fakturoid stored (> 0.02)
- Line count differs between extraction and stored expense

## Cleanup behavior

By default the script leaves the created expenses and subjects in place so you
can eyeball them in the Fakturoid UI. Pass `--cleanup` to delete them at the end:

- All expenses referenced by `export.json` are `DELETE`d.
- Subjects the script itself created (i.e. not present before the import phase)
  are `DELETE`d. Pre-existing subjects are never touched.

If a cleanup delete fails, it's logged as a warning — the run still exits on
the validation/idempotency result, not on cleanup.

## Rate limits

Fakturoid caps at 100 req/hour. A full run on the 11-PDF fixture set uses
roughly 50–60 requests (subject list + create + expense create + per-record
validation GET + optional cleanup DELETEs). Well under the cap; no throttling
expected.
