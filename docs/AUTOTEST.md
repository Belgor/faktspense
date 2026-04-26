# Live e2e autotest — iteration tool for agentic development

`scripts/e2e_real.py` is the live integration test. It really extracts
invoice data from PDFs (Claude API), really creates subjects and
expenses in Fakturoid, then GETs each one back to verify the data is
there and correct. Every check is recorded as a structured event in
`report.json`; every Fakturoid HTTP request is appended to `api.log`.

The intended workflow for an agent iterating on faktspense:

1. Make a code change.
2. Run `uv run python scripts/e2e_real.py --cleanup`.
3. Read `test_data_real/_faktspense_run/report.json` (or `report.md`).
4. Fix any failed checks; loop.

Not wired into pytest — `uv run pytest` never touches real APIs.

## Prerequisites

A **dedicated Fakturoid test account** (Fakturoid has no sandbox — use a
separate real account you don't mind filling with test data).

The script reads four `TEST_`-prefixed env vars (the prefix keeps
test-account credentials lexically distinct from the unprefixed
production credentials the CLI reads):

    TEST_FAKTUROID_CLIENT_ID
    TEST_FAKTUROID_CLIENT_SECRET
    TEST_FAKTUROID_SLUG            # must be a dedicated test account
    TEST_ANTHROPIC_API_KEY

`sbx secret set` does **not** work for these — it only supports a fixed
allowlist of named services (anthropic/aws/github/openai/…) and even
those are exposed only to the network proxy, not as env vars inside the
sandbox. The setup below uses a host file mounted read-only into the
sandbox instead.

Input PDFs live in `test_data_real/` (gitignored).

### One-time host setup

Create the env file in your host home dir, mode 600:

```bash
mkdir -p ~/.config/faktspense
chmod 700 ~/.config/faktspense

cat > ~/.config/faktspense/.env.autotest <<'EOF'
export TEST_FAKTUROID_CLIENT_ID=...
export TEST_FAKTUROID_CLIENT_SECRET=...
export TEST_FAKTUROID_SLUG=...
export TEST_ANTHROPIC_API_KEY=sk-ant-...
EOF

chmod 600 ~/.config/faktspense/.env.autotest
```

The file lives only on the host; it never enters the sandbox image,
shell history, or git.

### Launching the sandbox with the env file mounted

`sbx run` accepts additional workspace mounts as trailing path args, with
`:ro` for read-only. Mount the parent dir read-only when launching:

```bash
sbx run claude . ~/.config/faktspense:ro
```

If your sandbox already exists and you want the mount to apply, recreate
it (`sbx rm <sandbox-name>` then the command above) — additional
workspaces are bound at sandbox-create time, not on attach.

After attach, verify the mount inside the sandbox:

```bash
ls -l ~/.config/faktspense/.env.autotest      # should show the file, ro
mount | grep faktspense                       # should show ro mount
```

### Loading the env vars before each autotest run

```bash
set -a
source ~/.config/faktspense/.env.autotest
set +a
```

`set -a` exports every variable assigned until `set +a`, so this works
whether or not your file uses the `export ` prefix. Verify:

```bash
env | grep '^TEST_FAKTUROID\|^TEST_ANTHROPIC' | sed 's/=.*/=<set>/'
```

The vars stay in the current shell only — they are not persisted to the
sandbox image.

## Run

Source the env file first (see "Loading the env vars" above), then:

```bash
# Full run, artifacts left behind in the test account for inspection:
uv run python scripts/e2e_real.py

# Full run + delete everything the script created at the end:
uv run python scripts/e2e_real.py --cleanup

# Resume from a partial run (uses sidecars in test_data_real/_faktspense_run/ as-is):
uv run python scripts/e2e_real.py --skip-extract

# Validate an existing import without importing again:
uv run python scripts/e2e_real.py --skip-extract --skip-import
```

One-liner that sources + runs (handy for re-runs in the same shell):

```bash
( set -a; source ~/.config/faktspense/.env.autotest; set +a;
  uv run python scripts/e2e_real.py --cleanup )
```

The script exits non-zero on any failed check; read
`test_data_real/_faktspense_run/report.json` (or `report.md`) for
details.

## Artifacts (in `test_data_real/_faktspense_run/`)

| File | Purpose |
|---|---|
| `<pdf_stem>_<sha8>.json` | One per PDF — the same sidecar layout the production CLI writes. Extracted fields (editable) plus a `fakturoid` block (`subject_id` / `expense_id` / `status` / `imported_at`). The full sha256 inside `id` is checked on re-runs to detect PDF content changes. |
| `.subjects_cache.json` | Isolated subject cache (does not touch `~/.cache/faktspense/`). |
| `report.json` | Structured run log — see schema below. **The primary artifact for an agent to read.** |
| `report.md` | Same data, rendered as a human-readable Markdown table + per-failure detail sections. |
| `api.log` | One line per Fakturoid HTTP request: timestamp, method, URL, status, latency. |

Persistence goes through the production `ExportStore`; the script does
not implement its own per-record I/O.

## `report.json` schema

```jsonc
{
  "started_at":   "2026-04-25T16:23:01Z",
  "finished_at":  "2026-04-25T16:24:18Z",
  "pdf_dir":      "/.../test_data_real",
  "work_dir":     "/.../test_data_real/_faktspense_run",
  "fakturoid_slug": "myslug",
  "args":         { "cleanup": false, "skip_extract": false, ... },
  "created_subject_ids": [123, 124],
  "records": {
    "<sha256>": {
      "record_id":          "<sha256>",
      "pdf_name":           "2023-07-04_...pdf",
      "invoice_number":     "230110678",
      "vendor_name":        "Kinetic s.r.o.",
      "vendor_ico":         "12345678",
      "expense_id":         99001,
      "subject_id":         123,
      "subject_was_created": true,
      "extraction_diffs":   ["total: extracted=1210, Fakturoid=1210.00"],
      "checks": [
        { "name": "extract",            "ok": true,  "detail": "...", "data": null },
        { "name": "expense_create",     "ok": true,  "detail": "..." },
        { "name": "validate_expense",   "ok": true,  "detail": "...",
          "data": { "fakturoid_response": { ...full GET body... } } },
        { "name": "validate_subject",   "ok": false, "detail": "IČO mismatch (...)",
          "data": { "subject_id": 123, "fakturoid_response": {...}, "action": "created" } },
        { "name": "idempotency",        "ok": true,  "detail": "..." }
      ]
    }
  }
}
```

A record's `hard_ok` (used by the summary printer) is `true` iff every
`check.ok` is `true`. Soft diffs in `extraction_diffs` are non-fatal —
they flag extraction-quality drift but don't fail the run.

## Checks performed

| Check | What it verifies |
|---|---|
| `extract`            | `ClaudeExtractor` returned a valid `ExtractedInvoice` for the PDF (or skipped because the sidecar already exists). |
| `expense_create`     | `ImportRunner.run_one` produced `status=imported` with an `expense_id`. |
| `validate_expense`   | GET `/expenses/{id}` returns matching `custom_id`, `number`, `subject_id`, and ≥ 1 attachment. |
| `validate_subject`   | GET `/subjects/{id}` returns matching IČO. Vendor-name divergence is reported as a soft diff (vendors often differ in legal-form punctuation). |
| `idempotency`        | Re-running `import` on the record raises `AlreadyImportedError`, never a second POST. |
| `cleanup_expense`    | (`--cleanup`) DELETE `/expenses/{id}` succeeded. |

## Reading the report from an agent

Common queries:

```bash
# Overall pass/fail
jq '.records | to_entries | map(.value | {pdf_name, hard_ok: (all(.checks[]; .ok))}) ' \
   test_data_real/_faktspense_run/report.json

# All failed checks, by record
jq '.records | to_entries[] | .value
    | { pdf: .pdf_name, fails: [ .checks[] | select(.ok == false) ] }
    | select(.fails | length > 0)' \
   test_data_real/_faktspense_run/report.json

# Inspect the Fakturoid response for a specific record's expense validation
jq '.records["<sha256>"].checks[] | select(.name == "validate_expense").data.fakturoid_response' \
   test_data_real/_faktspense_run/report.json

# Was this run rate-limited?
grep ' 429 ' test_data_real/_faktspense_run/api.log
```

## Cleanup behavior

By default the script leaves the created expenses and subjects in place
so you can eyeball them in the Fakturoid UI. Pass `--cleanup` to delete
them at the end:

- All expenses referenced by the sidecars are `DELETE`d.
- Subjects the script itself created (not present before the import
  phase) are `DELETE`d. Pre-existing subjects are never touched.

If a cleanup delete fails, it's recorded as a failed `cleanup_expense`
check — the run still exits on validation/idempotency result, not on
cleanup.

## Rate limits

Fakturoid caps at 100 req/hour. A full run on the 11-PDF fixture set
uses roughly 60–80 requests (subject list + create + expense create +
per-record validation GETs for both expense and subject + optional
cleanup DELETEs). Well under the cap; if you do hit 429, check
`api.log`.
