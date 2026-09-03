# Android Emulator Instagram Reels Collector Design

## Goal

Add a fourth, fully isolated collection version for an Android Studio emulator that has an authenticated Instagram app session.  It must automate three read-only workflows:

1. browse and collect new Reels from the Reels feed; and
2. search one or more hashtags and collect new Reels from their Reels results.

The collector only launches the app, searches, opens surfaces, taps metric panels, and scrolls.  It must never like, follow, comment, post, send, or otherwise change the Instagram account.  Login, challenge, CAPTCHA, and rate-limit screens are recorded and stop the affected run without attempting a bypass.

## Project boundaries

Create an independent `android_emulator_version/` directory.  It owns its source code, tests, virtual environment, ADB work directory, and `data_android/` output.  It does not import, edit, lock, or overwrite `python_version/`, `python_no_login_version/`, or their data files.

The initial implementation uses Android SDK `adb` and `uiautomator`.  The collection core depends only on an `AndroidDriver` protocol.  The ADB implementation is the first driver; a later Appium implementation can satisfy the same protocol without changing parsing, collection state, or export code.

## Components

| Component | Responsibility |
| --- | --- |
| `android_collector/driver.py` | Defines the driver protocol: app launch, UI dump, tap, text entry, scroll, deep-link opening, and screenshot capture. |
| `android_collector/adb_driver.py` | Runs ADB commands against one selected emulator.  It uses a collector-owned ADB user directory so the desktop user's Android configuration is not changed. |
| `android_collector/ui_parser.py` | Parses UIAutomator XML, normalises exact visible integers, recognises Korean and English Instagram labels, and preserves unrecognised UI text. |
| `android_collector/workflows.py` | Implements new-only feed and hashtag traversal while respecting per-item delays and stop conditions. |
| `android_collector/new_only_schedule.py` | Runs new-only fashion and beauty keyword windows using the Python-version keyword vocabulary. |
| `android_collector/store.py` | Writes atomic CSV, JSON, and true XLSX exports plus one XML and PNG evidence pair per observed Reel. |
| `collect_android_reels.py` | Command-line entry point. |

## Data flow

1. A preflight checks that exactly one selected ADB emulator is online, Instagram is installed, and the UI does not show a login, challenge, or rate-limit surface.
2. A workflow opens the requested app surface: the Reels feed or an Instagram hashtag search.
3. For each visible Reel, the collector saves the raw UI XML and screenshot, then parses visible author, caption, audio, interaction counts, and the `Likes and plays` metric panel.
4. Numeric values receive only fully displayed non-negative integers such as `32,357`. Compact values such as `5.7K` are preserved as Android evidence and do not become inferred integers.
5. The public `data_android/reels.csv`, `reels.json`, and `reels.xlsx` exports use the same row-oriented fields and order as `python_version`: collection number, elapsed collection days, standard Reel fields, reaction rate, and metric-change fields. All three are rebuilt atomically after checkpoints and at normal exit.

Public rows include only fields that have a compatible meaning on the visible Android surface. The collector opens the non-mutating `Likes and plays` sheet from the like-count control and reads its exact `like_count` and `view_count`; it also records an explicit hidden-like-count notice when shown. Values not reliably visible there, such as user ID, upload time, and follower count, are empty. The compact side-rail `Likes and plays` value is never treated as a view count without that detail sheet.

`data_android/.collector/android_observations.json` owns Android-specific collection metadata: `source_mode` (`feed` or `hashtag`), `source_query`, explicit `reel_fingerprint`, raw-text metric values, all recognised app-only metrics (including `likes_and_plays_count`, exact `share_count`, and `like_count_is_private`), and evidence paths. The standard public `reels.*` history does not add an Android-only `share_count` column; its exact value remains in this evidence-friendly internal record. `users.csv`, `users.json`, and `users.xlsx` hold author observation history in the compatible user layout. Feed and hashtag discovery do not use Instagram's Copy Link action, because the agreed automation scope is limited to searching, opening detail surfaces, and scrolling. Consequently a discovered item without a UI-exposed permalink is exported with an empty public `url` and the internal fingerprint prevents it from being saved again on later new-only runs.

## Commands

```powershell
cd C:\Instagram-crawling\android_emulator_version
.\.venv\Scripts\python.exe .\collect_android_reels.py feed --max-items 50
.\.venv\Scripts\python.exe .\collect_android_reels.py collect --hashtag-query '패션 OR ootd' --max-items 50
.\.venv\Scripts\python.exe .\collect_android_reels.py fashion-beauty --six-hour-new-only
```

All commands accept `--device-id` for a specific emulator.  The collector reports the manual action required for a login or challenge rather than interacting with those screens.

## Failure handling

- ADB missing, offline device, multiple emulators without `--device-id`, or a missing Instagram package fails before collection.
- Login, challenge, CAPTCHA, or rate limit stops the run and writes a status file plus the last evidence artifact.
- An unexpected layout stores evidence and marks that observation as `layout_unrecognised`; it never invents missing values.
- A single transient UI-dump or navigation failure retries only a bounded number of times with a delay.  It never retries account-access errors.
- Scrolling is paced and bounded by `--max-items`; a repeated fingerprint terminates feed/hashtag traversal instead of looping indefinitely.

## Verification

Automated unit tests use saved UI XML fixtures and a fake driver; no test opens Instagram or needs an emulator. They cover exact-number parsing, Korean/English label recognition, compact-number refusal, metric-panel extraction, new-only discovery fingerprints, account-access stop conditions, command parsing, and atomic export shape. A manual smoke-test checklist covers the connected Android Studio emulator, an already logged-in app session, one item from each workflow, and evidence artifact creation.
