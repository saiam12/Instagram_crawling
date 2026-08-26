# Public Export Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Normalize public Instagram exports and provide a safe command that merges `reels_updated.xlsx` into the active Reel history before deleting it.

**Architecture:** Keep `.collector` CSV files as collector-only raw histories. Build every public users or Reels file from a shared display projection, so the CSV, JSON, and XLSX variants have identical columns and record shape. The reconciliation command imports only missing or blank values from the temporary workbook into the raw Reel history, then rebuilds public files.

**Tech Stack:** Python 3.12, standard-library CSV/JSON/ZIP XML handling, unittest.

**Spec:** `docs/superpowers/specs/2026-08-26-public-export-reconciliation.md`

## Global Constraints

- Preserve raw numeric values in `.collector` histories.
- Do not alter status or lock JSON files.
- Delete `reels_updated.xlsx` only after its contents are reconciled and public exports are saved.
- Add tests before production changes and verify the complete collector/export test suite.

---

### Task 1: Shared public-output projection

**Files:**

- Modify: `python_version/collectors/instagram_reels_browser.py`
- Test: `python_version/collectors/test_instagram_reels_browser.py`

**Interfaces:**

- Consumes: `long_rows_to_wide(rows)` and `_xlsx_project_rows(stem, matrix)`.
- Produces: a shared projected matrix used by public Reel CSV, JSON, and XLSX writers.

- [ ] **Step 1: Write failing export-bundle tests**

Assert that a two-snapshot Reel produces matching CSV/JSON/XLSX headers, including `2nd collect_days_since_previous`, and that a public users CSV matches its XLSX row-history header.

- [ ] **Step 2: Run focused tests to verify failure**

Run: `python -m unittest collectors.test_instagram_reels_browser`

Expected: the current CSV/JSON headers lack the XLSX-only elapsed-time field and public users CSV remains wide.

- [ ] **Step 3: Implement display projection helpers**

Build projected rows once from the existing XLSX projection, use them in public CSV/JSON/XLSX writers, and keep Reel raw rows in `.collector/reels_history_active.csv`.

- [ ] **Step 4: Run focused tests to verify success**

Run: `python -m unittest collectors.test_instagram_reels_browser`

Expected: public output tests pass with matching headers and values.

### Task 2: User-history migration and public users export

**Files:**

- Modify: `python_version/collectors/instagram_follower_enricher.py`
- Modify: `python_version/collectors/instagram_reels_browser.py`
- Test: `python_version/collectors/test_instagram_reels_browser.py`

**Interfaces:**

- Consumes: legacy public `users.csv` when active history is absent.
- Produces: `.collector/users_history_active.csv` plus synchronized public `users.csv` and `users.xlsx`.

- [ ] **Step 1: Write failing migration test**

Seed a wide legacy `users.csv`, invoke the public users export, and assert that it creates an active-history copy and rewrites public CSV/XLSX to the seven row-history fields.

- [ ] **Step 2: Run test to verify failure**

Run: `python -m unittest collectors.test_instagram_reels_browser`

Expected: no active user-history file exists and public CSV remains wide.

- [ ] **Step 3: Implement migration and bundle export**

Use the active-history file for follower enrichment after one-time legacy migration; regenerate both public users files from the same projected matrix.

- [ ] **Step 4: Run test to verify success**

Run: `python -m unittest collectors.test_instagram_reels_browser`

Expected: active history remains wide while public CSV/XLSX are identical row layouts.

### Task 3: Reel-updated reconciliation command

**Files:**

- Modify: `python_version/collectors/instagram_reels_browser.py`
- Modify: `python_version/scripts/instagram_reels_python.py`
- Modify: `python_version/README.md`
- Test: `python_version/collectors/test_instagram_reels_browser.py`

**Interfaces:**

- Consumes: `<data-dir>/reels_updated.xlsx` and `.collector/reels_history_active.csv`.
- Produces: reconciled raw history, synchronized public Reel outputs, and removal of the temporary updated workbook.

- [ ] **Step 1: Write failing reconciliation tests**

Create an active history plus an updated workbook containing a new Reel or a blank field that the workbook supplies. Assert that reconciliation merges it, rewrites public outputs, and removes the temporary workbook only after success.

- [ ] **Step 2: Run test to verify failure**

Run: `python -m unittest collectors.test_instagram_reels_browser`

Expected: no reconciliation API or `reconcile` command exists.

- [ ] **Step 3: Implement XLSX import, merge, and command**

Decode the workbook's date and numeric cells, match snapshots by canonical URL plus collection time, fill missing raw values without overwriting nonblank history, regenerate exports, then delete `reels_updated.xlsx`. Add `reconcile` to the command launcher and PowerShell documentation.

- [ ] **Step 4: Run focused tests to verify success**

Run: `python -m unittest collectors.test_instagram_reels_browser`

Expected: new data is preserved, matching history is not duplicated, and the updated workbook is removed after successful export.

### Task 4: Regenerate and verify the current outputs

**Files:**

- Modify: `python_version/data_web/users.csv`
- Modify: `python_version/data_web/users.xlsx`
- Modify: `python_version/data_web/reels.csv`
- Modify: `python_version/data_web/reels.json`
- Modify: `python_version/data_web/reels.xlsx`
- Delete: `python_version/data_web/reels_updated.xlsx`

- [ ] **Step 1: Back up the current public exports**

Copy the affected public files to a timestamped `.recovery_backups` directory before reconciliation.

- [ ] **Step 2: Run the reconciliation command**

Run: `./collector.ps1 reconcile`

Expected: exports have matching headers and `reels_updated.xlsx` is removed only after reconciliation reports success.

- [ ] **Step 3: Verify artifacts and full test suite**

Run: `python -m unittest collectors.test_instagram_reels_browser exporters.test_instagram_collector`

Use the spreadsheet artifact tool to inspect output headers, representative rows, and formula errors, then render the `users` and `reels` sheets.
