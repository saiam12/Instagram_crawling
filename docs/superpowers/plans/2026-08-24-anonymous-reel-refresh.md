# Anonymous Reel Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow `refresh --no-login` to append a safe public-metric snapshot without overwriting existing Reel metadata or stopping on an unavailable follower count.

**Architecture:** Add a pure record builder that combines the prior URL history row with newly observed exact public metrics.  The existing long-history-to-wide exporter continues to make the second snapshot columns, elapsed time, and display deltas; the no-login pipeline bypasses the `FollowerEnricher` and uses a bounded direct API retry instead.

**Tech Stack:** Python 3.12+, asyncio, Playwright, unittest, existing CSV/XLSX exporter.

**Spec:** `docs/superpowers/specs/2026-08-24-anonymous-refresh.md`

## Global Constraints

- Modify only `python_version` collection code and its documentation/tests; preserve unrelated dirty working-tree changes.
- Use only raw Instagram Network integer fields for metrics; never parse rendered compact labels.
- Preserve normal logged-in behavior and source-field checks.
- No commit or branch manipulation: the requested Python tree is currently user-owned and untracked on `main`.

---

### Task 1: Anonymous snapshot construction and follower retry

**Files:**
- Modify: `python_version/collectors/instagram_reels_browser.py`
- Test: `python_version/collectors/test_instagram_reels_browser.py`

**Interfaces:**
- Produces `build_anonymous_refresh_record(existing, observed) -> dict[str, Any]`.
- Produces `request_anonymous_follower_count(page, username) -> dict[str, Any]`.
- Consumes existing `build_collected_record`, `exact_nonnegative_integer`, and `request_web_follower_count`.

- [x] **Step 1: Write failing tests**

```python
def test_anonymous_refresh_preserves_static_metadata_and_blanks_follower() -> None:
    refreshed = build_anonymous_refresh_record(existing, observed)
    self.assertEqual(refreshed["audio_name"], "original audio")
    self.assertEqual(refreshed["view_count"], 1_100)
    self.assertEqual(refreshed["follower_count"], "")

async def test_anonymous_follower_retry_stops_after_three_transient_errors(self) -> None:
    result = await request_anonymous_follower_count(page, "example")
    self.assertEqual(page.goto_calls, 3)
    self.assertEqual(result["status"], "web_error")
```

- [x] **Step 2: Run the targeted tests and verify they fail because the functions do not exist.**

Run: `python -m unittest collectors.test_instagram_reels_browser.CollectorUtilityTests.test_anonymous_refresh_preserves_static_metadata_and_blanks_follower collectors.test_instagram_reels_browser.CollectorAsyncTests.test_anonymous_follower_retry_stops_after_three_transient_errors`

- [x] **Step 3: Implement the minimal pure builder and bounded retry.**

```python
def build_anonymous_refresh_record(existing, observed):
    refreshed = {field: existing.get(field, "") for field in CSV_FIELDS}
    for field in ["collected_at", "url"]:
        refreshed[field] = observed.get(field, refreshed[field])
    for field in ["view_count", "like_count", "comment_count", "repost_count"]:
        refreshed[field] = exact_nonnegative_integer(observed.get(field))
    refreshed["follower_count"] = ""
    refreshed["days_since_upload"] = days_since_upload(refreshed["uploaded_at"], refreshed["collected_at"])
    return refreshed
```

Only retry `web_error` up to three total calls; return every other result unchanged.

- [x] **Step 4: Re-run the targeted tests and verify they pass.**

### Task 2: Route anonymous refresh through the safe snapshot path

**Files:**
- Modify: `python_version/collectors/instagram_reels_browser.py`
- Modify: `python_version/README.md`
- Test: `python_version/collectors/test_instagram_reels_browser.py`

**Interfaces:**
- Consumes the two Task 1 helpers.
- Uses `LongReelStore.rows` to select the latest prior history row by canonical Reel URL.
- Produces a new long-history row accepted by the existing wide exporter.

- [x] **Step 1: Write a failing storage/export test.**

```python
def test_anonymous_refresh_creates_a_second_snapshot_with_elapsed_days_and_display_delta(self) -> None:
    fields, rows = long_rows_to_wide([initial, anonymous_refresh])
    self.assertEqual(rows[0]["2nd collect_view_count"], 1_100)
    self.assertEqual(projected[1][projected[0].index("2nd collect_days_since_previous")], "+2day")
```

- [x] **Step 2: Run the test and verify it fails because the anonymous snapshot has not yet been integrated with the long-history path.**

- [x] **Step 3: Implement the no-login refresh branch.**

```python
anonymous_refresh = bool(options.no_login and refresh_urls)
if anonymous_refresh:
    # require a prior history row, fetch exact public counts, blank follower on
    # every unavailable result, append the safe combined snapshot, and continue.
else:
    # preserve the exact logged-in collection gate unchanged.
```

The branch must use the detail response and creator-Reels `play_count` lookup,
but must not start `FollowerEnricher` or classify an unavailable follower as a
job-wide error.

- [x] **Step 4: Update the README with the no-login refresh command and its exact limitations.**

- [x] **Step 5: Run the focused test module and compile the changed Python files.**

### Task 3: Final verification and review

**Files:**
- Review: `python_version/collectors/instagram_reels_browser.py`
- Review: `python_version/collectors/test_instagram_reels_browser.py`
- Review: `python_version/README.md`

- [x] **Step 1: Run the full Python test suite.**

Run: `python -m unittest collectors.test_instagram_reels_browser exporters.test_instagram_collector`

- [x] **Step 2: Compile all changed Python modules.**

Run: `python -m py_compile collectors/instagram_reels_browser.py collectors/test_instagram_reels_browser.py scripts/instagram_reels_python.py`

- [x] **Step 3: Request an independent code review and address any confirmed findings.**

- [x] **Step 4: Report the exact command and the no-login follower behavior.**
