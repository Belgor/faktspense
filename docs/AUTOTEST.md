# Live e2e autotest — iteration tool for agentic development

`scripts/e2e_real.py` is the live integration test. It drives the production
`faktspense` CLI (`faktspense extract` + `faktspense import`) against a
dedicated Fakturoid test account using real PDFs, then GETs each created
expense and subject back to verify the data. Every check is recorded in
`report.json`; every Fakturoid HTTP request is appended to `api.log`.

The intended workflow for an agent iterating on faktspense:

1. Make a code change.
2. Run `uv run python scripts/e2e_real.py --cleanup`.
3. Read `test_data_real/_faktspense_run/report.md` (or `report.json`).
4. Fix any failed checks; loop. Use `--skip-extract` to reuse sidecars when
   only fixing import or validation issues.

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
mkdir -p /home/belgor/.config/faktspense
chmod 700 /home/belgor/.config/faktspense

cat > /home/belgor/.config/faktspense/.env.autotest <<'EOF'
export TEST_FAKTUROID_CLIENT_ID=...
export TEST_FAKTUROID_CLIENT_SECRET=...
export TEST_FAKTUROID_SLUG=...
export TEST_ANTHROPIC_API_KEY=sk-ant-...
EOF

chmod 600 /home/belgor/.config/faktspense/.env.autotest
```

The file lives only on the host; it never enters the sandbox image,
shell history, or git.

### Launching the sandbox with the env file mounted

`sbx run` accepts additional workspace mounts as trailing path args, with
`:ro` for read-only. Mount the parent dir read-only when launching:

```bash
sbx run claude . /home/belgor/.config/faktspense:ro
```

If your sandbox already exists and you want the mount to apply, recreate
it (`sbx rm <sandbox-name>` then the command above) — additional
workspaces are bound at sandbox-create time, not on attach.

After attach, verify the mount inside the sandbox:

```bash
ls -l /home/belgor/.config/faktspense/.env.autotest   # should show the file, ro
mount | grep faktspense                               # should show ro virtiofs mount
```

### Loading the env vars before each autotest run

```bash
set -a
source /home/belgor/.config/faktspense/.env.autotest
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

# Full run with Sonnet second-pass semantic verification on extraction:
uv run python scripts/e2e_real.py --verify

# Full run + delete everything the script created at the end:
uv run python scripts/e2e_real.py --cleanup

# Resume from a partial run (uses sidecars in test_data_real/_faktspense_run/ as-is):
uv run python scripts/e2e_real.py --skip-extract

# Validate an existing import without importing again:
uv run python scripts/e2e_real.py --skip-extract --skip-import
```

One-liner that sources + runs (handy for re-runs in the same shell):

```bash
( set -a; source /home/belgor/.config/faktspense/.env.autotest; set +a;
  uv run python scripts/e2e_real.py --cleanup )
```

The script exits non-zero on any failed check; read
`test_data_real/_faktspense_run/report.md` (or `report.json`) for details.

## Flags

| Flag | Effect |
|---|---|
| `--verify` | Passes `--verify` to `faktspense extract`, enabling the Sonnet second-pass semantic check. Results appear in `report.md` Section A. |
| `--cleanup` | DELETE every expense and subject the script created, after all other phases complete. |
| `--skip-extract` | Skip `faktspense extract`; reuse sidecars already in `--work-dir`. |
| `--skip-import` | Skip `faktspense import`; assume sidecars already have `expense_id`s. |
| `--skip-validate` | Skip GET-back validation of expenses and subjects. |
| `--skip-idempotency` | Skip the idempotency re-run check. |
| `--pdf-dir PATH` | Input PDFs (default: `test_data_real/`). |
| `--work-dir PATH` | Scratch dir for sidecars, subject cache, and reports (default: `<pdf-dir>/_faktspense_run/`). |

## Artifacts (in `test_data_real/_faktspense_run/`)

| File | Purpose |
|---|---|
| `<pdf_stem>_<sha8>.json` | One per PDF — the same sidecar layout the production CLI writes. Extracted fields (editable) plus a `fakturoid` block (`subject_id` / `expense_id` / `status` / `imported_at`). |
| `.subjects_cache.json` | Isolated subject cache (does not touch `~/.cache/faktspense/`). |
| `report.json` | Structured run log. |
| `report.md` | Human-readable Markdown report (five sections — see below). |
| `api.log` | One line per Fakturoid HTTP request: timestamp, method, URL, status, latency. |

## `report.md` structure

| Section | Content |
|---|---|
| **A — Extracted invoice data** | Per-PDF table: vendor, IČO, invoice number, issued date, currency, line count, total, arithmetic/Sonnet warnings. |
| **B — Import results** | Per-invoice table: `expense_id`, `subject_id`, whether subject was created or reused, Fakturoid-side total/lines/attachment presence. |
| **C — Summary table** | Per-invoice pass/fail grid across all check names. |
| **D — Extraction-quality diffs** | Non-fatal differences between extracted values and Fakturoid's stored values (e.g. total rounding, vendor name punctuation). |
| **E — Failure details** | Full detail for every hard-failed invoice, including failed check messages and CLI output snippets. |

## `report.json` schema

```jsonc
{
  "started_at":   "2026-04-25T16:23:01Z",
  "finished_at":  "2026-04-25T16:24:18Z",
  "pdf_dir":      "/.../test_data_real",
  "work_dir":     "/.../test_data_real/_faktspense_run",
  "fakturoid_slug": "myslug",
  "args":         { "cleanup": false, "verify": false, "skip_extract": false, ... },
  "created_subject_ids": [123, 124],
  "records": {
    "<sha256>": {
      "record_id":          "<sha256>",
      "pdf_name":           "2023-07-04_...pdf",
      "invoice_number":     "230110678",
      "vendor_name":        "Kinetic s.r.o.",
      "vendor_ico":         "12345678",
      "vendor_dic":         "CZ12345678",
      "issued_on":          "2023-07-04",
      "due_date":           "2023-07-18",
      "currency":           "CZK",
      "line_count":         3,
      "extracted_total":    "1210",
      "arithmetic_warnings": [],
      "sonnet_ok":          null,
      "sonnet_issues":      [],
      "expense_id":         99001,
      "subject_id":         123,
      "subject_was_created": true,
      "extraction_diffs":   ["total: extracted=1210, Fakturoid=1210.00"],
      "checks": [
        { "name": "extract",            "ok": true,  "detail": "extracted: 230110678 — 'Kinetic s.r.o.'" },
        { "name": "expense_create",     "ok": true,  "detail": "imported expense_id=99001 subject_id=123" },
        { "name": "validate_expense",   "ok": true,  "detail": "expense matches sidecar",
          "data": { "fakturoid_response": { ...full GET body... } } },
        { "name": "validate_subject",   "ok": true,  "detail": "subject 123 (created) matches IČO 12345678",
          "data": { "subject_id": 123, "fakturoid_response": {...}, "action": "created" } },
        { "name": "idempotency",        "ok": true,  "detail": "re-import skipped — expense_id unchanged (99001)" }
      ],
      "cli_extract_output": "...",
      "cli_import_output":  "..."
    }
  }
}
```

## Checks performed

| Check | What it verifies |
|---|---|
| `extract` | `faktspense extract` wrote a valid sidecar for the PDF (or the sidecar was reused on `--skip-extract`). |
| `expense_create` | `faktspense import` produced `status=imported` with an `expense_id`. |
| `validate_expense` | GET `/expenses/{id}` returns matching `custom_id`, `original_number`, `subject_id`, and ≥ 1 attachment. |
| `validate_subject` | GET `/subjects/{id}` returns matching IČO. Vendor-name divergence is recorded as a soft diff. |
| `idempotency` | Re-running `faktspense import` on the same sidecar directory leaves every `expense_id` unchanged — no double-POST occurred. |
| `cleanup_expense` | (`--cleanup`) DELETE `/expenses/{id}` succeeded. |

## How the script works

The script is structured as discrete phases. Each phase calls the real
`faktspense` CLI as a subprocess (so it tests the exact same code path a user
runs). Only the validation and cleanup phases call the Fakturoid API directly
(using the same `FakturoidClient` used by the CLI).

1. **phase_extract** — runs `uv run faktspense extract <pdf-dir> -o <work-dir>`
   (plus `--verify` if `--verify` was passed). Reloads sidecars afterward to
   populate the report's extraction-data fields.
2. **phase_import** — snapshots the subject list, then runs
   `uv run faktspense import <work-dir> --auto-create-subjects --force-review`.
   Refreshes the subject list afterward to detect newly created subjects.
3. **phase_validate_expenses / phase_validate_subjects** — GET each created
   expense and subject back from Fakturoid; compare against the sidecar.
4. **phase_idempotency** — snapshots `expense_id`s, re-runs
   `faktspense import`, compares expense_ids. A changed `expense_id` means a
   double-POST occurred.
5. **phase_cleanup** — (if `--cleanup`) DELETE created expenses and subjects.

## Reading the report from an agent

```bash
# Overall pass/fail per invoice
jq '.records | to_entries[] | {pdf: .value.pdf_name, ok: (all(.value.checks[]; .ok))}' \
   test_data_real/_faktspense_run/report.json

# All failed checks
jq '.records | to_entries[] | .value
    | { pdf: .pdf_name, fails: [ .checks[] | select(.ok == false) ] }
    | select(.fails | length > 0)' \
   test_data_real/_faktspense_run/report.json

# Fakturoid response for a specific expense
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

Fakturoid caps at 100 req/hour. A full run on a small fixture set uses
roughly 30–60 requests (subject list + create + expense create +
per-record validation GETs for both expense and subject + optional
cleanup DELETEs). Well under the cap; if you do hit 429, check `api.log`.
