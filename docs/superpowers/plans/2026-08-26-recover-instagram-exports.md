# Instagram Export Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the interrupted Instagram collection into consistent public CSV, JSON, and XLSX exports without losing the prior files.

**Architecture:** Build a staged dataset by loading the internal long-form history and replaying the pending JSONL journal using the collector's existing record-integration rules. Create the two XLSX workbooks from the staged matrices with `@oai/artifact-tool`; only replace public files after the staged counts and workbook exports are valid. Preserve timestamped copies of every replaced source and journal file.

**Tech Stack:** Project Python collector helpers; bundled Node.js with `@oai/artifact-tool`; PowerShell for verified file replacement.

**Spec:** User request in the current Codex task (2026-08-26): "복구해줘".

## Global Constraints

- Merge `data_web/.collector/reels_history_active.csv.pending.jsonl` into the active history using `integrate_long_collected_record`.
- Recover the complete public bundle: `reels.csv`, `reels.json`, `reels.xlsx`, and `users.xlsx`.
- Create a timestamped backup before replacing any existing public file or consuming the journal.
- Use `@oai/artifact-tool` for both XLSX outputs.
- Expected recovered reel set: 395 unique URLs from 419 long-form snapshots; preserve all 512 user rows.

---

### Task 1: Stage and validate the recovered records

**Files:**
- Create: `.codex_tmp/recover_prepare.py`
- Create: `.codex_tmp/staging/recovery_metadata.json`
- Create: `.codex_tmp/staging/reels_matrix.json`
- Create: `.codex_tmp/staging/users_matrix.json`
- Read: `python_version/data_web/.collector/reels_history_active.csv`
- Read: `python_version/data_web/.collector/reels_history_active.csv.pending.jsonl`
- Read: `python_version/data_web/users.csv`

**Interfaces:**
- Consumes: `read_csv_objects`, `integrate_long_collected_record`, `long_rows_to_wide`, `_xlsx_project_rows`.
- Produces: staged public CSV/JSON, typed workbook matrices, and metadata with source and recovered row counts.

- [ ] **Step 1: Create the staging script**

  Load the active history, replay every non-empty pending JSON line, and stop with a non-zero exit code unless the recovered state is exactly 419 long rows and 395 unique URLs. Generate the public wide CSV/JSON and the two `_xlsx_project_rows` matrices in `.codex_tmp/staging`.

- [ ] **Step 2: Run the staging script**

  Run: `python_version/.venv/Scripts/python.exe .codex_tmp/recover_prepare.py`

  Expected: exit code 0 and metadata reporting 38 journal rows, 419 recovered history rows, 395 public reel rows, and 512 user rows.

### Task 2: Create recoverable XLSX outputs and backups

**Files:**
- Create: `.codex_tmp/recover_exports.mjs`
- Create: `outputs/recovery-20260826/reels_recovered.xlsx`
- Create: `outputs/recovery-20260826/users_recovered.xlsx`
- Create: `python_version/data_web/.recovery_backups/<timestamp>/`
- Modify: `python_version/data_web/reels.csv`
- Modify: `python_version/data_web/reels.json`
- Modify: `python_version/data_web/reels.xlsx`
- Modify: `python_version/data_web/users.xlsx`
- Modify: `python_version/data_web/.collector/reels_history_active.csv`
- Delete after backup: `python_version/data_web/.collector/reels_history_active.csv.pending.jsonl`

**Interfaces:**
- Consumes: staged matrices and staged public files from Task 1.
- Produces: validated XLSX copies in `outputs/recovery-20260826/`, backed-up and atomically replaced public exports, and the recovered active history.

- [ ] **Step 1: Write both workbooks with artifact-tool**

  Create `reels` and `users` worksheets, write the staged matrices in blocks, keep IDs as text, store timestamps as `Date` values formatted `yyyy-mm-dd hh:mm:ss`, store counts as numbers formatted `#,##0`, add a table/filter row, freeze the header, and export to `outputs/recovery-20260826/`.

- [ ] **Step 2: Verify staged workbook contents before replacement**

  Import each output with artifact-tool and assert `reels` has 395 data rows and `users` has 512 data rows. Scan both workbooks for `#REF!`, `#DIV/0!`, `#VALUE!`, `#NAME?`, and `#N/A`.

- [ ] **Step 3: Back up and replace public files**

  Copy the five existing data files plus the pending journal to one timestamped recovery-backup directory. Replace the active history, public CSV, public JSON, and both XLSX files from their staged or verified outputs. Delete the original pending journal only after the replacement copies exist and the new active history contains all 419 snapshots.

### Task 3: Validate the repaired outputs

**Files:**
- Read: `python_version/data_web/reels.xlsx`
- Read: `python_version/data_web/users.xlsx`
- Read: `python_version/data_web/reels.csv`
- Read: `python_version/data_web/.collector/reels_history_active.csv`

**Interfaces:**
- Consumes: repaired files from Task 2.
- Produces: count reconciliation and PNG renders for visual QA.

- [ ] **Step 1: Reconcile file counts**

  Assert 419 active-history rows, 395 unique active URLs, 395 public CSV rows, 395 XLSX reel rows, and 512 user XLSX rows.

- [ ] **Step 2: Render both workbooks**

  Render the `reels` header and last data rows, plus the `users` header and last data rows. Inspect each PNG to confirm visible headers, untruncated dates/counts, and no blank or malformed sheet.

- [ ] **Step 3: Keep the backup and remove only temporary builders**

  Preserve `python_version/data_web/.recovery_backups/<timestamp>/`; delete `.codex_tmp` only after every reconciliation assertion succeeds.
