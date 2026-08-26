# Fashion and Beauty Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `collector.ps1 fashion` command that alternates fashion and beauty discovery, schedules six snapshots per Reel, and publishes isolated domain-specific exports.

**Architecture:** A pure scheduler module owns time calculations, keyword rotation, upload-age eligibility, and 30-minute window caps. An asynchronous supervisor serially calls the existing generic collector with a separate fashion or beauty workspace, then atomically publishes each workspace’s generated files under `fashion_*` or `beauty_*`; the default collector paths remain unchanged.

**Tech Stack:** Python 3.13, `asyncio`, standard-library `unittest`, the existing Playwright collector, PowerShell, and the current custom XLSX exporter.

**Spec:** `docs/superpowers/specs/2026-08-26-fashion-24h-collection-design.md`

## Global Constraints

- Run for 16 hours total; perform discovery only during the first 8 hours.
- Use six absolute offsets per successful initial Reel: `0`, `+30m`, `+1h`, `+2h`, `+4h`, and `+8h`.
- Alternate whole 30-minute discovery windows: fashion first, beauty second, repeating; each active domain has a minimum target of 50 new Reels and a strict cap of 500 new Reels.
- Process due snapshots for both domains before new discovery, in ascending scheduled-time order, with no concurrent browser collection.
- Accept new candidates only when their upload age at initial capture is no more than 30 days; do not apply that limit to recollections.
- Keep raw histories independent in `data_web/.datasets/fashion/.collector` and `data_web/.datasets/beauty/.collector`.
- Publish `fashion_reels.csv/json/xlsx`, `fashion_users.csv/xlsx`, `fashion_collector_status.json`, and corresponding `beauty_*` files in `data_web` without changing `reels.*` or `users.*`.
- Keep existing Reel pacing at least 2 seconds and follower pacing/caching at 8 seconds; add no third-party packages.

---

## File Structure

- Create `python_version/collectors/fashion_beauty_scheduler.py` for pure policies, data-set paths, due-job generation, alternating-window choice, and keyword constants.
- Create `python_version/collectors/fashion_beauty_collection.py` for the 16-hour supervisor, private supervisor lock, generic collector invocation, status reporting, and atomic publishing.
- Create `python_version/collectors/test_fashion_beauty_collection.py` for deterministic unit tests with temporary directories and a fake collector invocation.
- Modify `python_version/collectors/instagram_reels_browser.py` to add opt-in `new_urls_only` and `disable_recollect_cooldown` behaviors needed only by the supervisor.
- Modify `python_version/collectors/test_instagram_reels_browser.py` to preserve and test default collector behavior alongside the two opt-in controls.
- Modify `python_version/scripts/instagram_reels_python.py`, `python_version/collector.ps1`, and `python_version/README.md` to expose and document the approved `fashion` command.

### Task 1: Define and test the pure two-domain scheduling policy

**Files:**
- Create: `python_version/collectors/fashion_beauty_scheduler.py`
- Create: `python_version/collectors/test_fashion_beauty_collection.py`

**Interfaces:**

```python
FASHION_KEYWORDS: Sequence[str]
BEAUTY_KEYWORDS: Sequence[str]
SNAPSHOT_OFFSETS: Sequence[timedelta]

@dataclass(frozen=True)
class DatasetConfig:
    name: Literal["fashion", "beauty"]
    data_root: Path
    keywords: Sequence[str]

@dataclass(frozen=True)
class RunConfig:
    data_root: Path
    duration_hours: float = 16
    discovery_hours: float = 8
    discovery_interval_minutes: float = 30
    new_items_per_window: int = 50
    max_new_items_per_window: int = 500
    max_upload_age_days: float = 30

@dataclass(frozen=True)
class DueJob:
    dataset: str
    url: str
    due_at: datetime

Signature: `due_jobs(dataset, rows, now) -> list[DueJob]`
Signature: `window_dataset(started_at, now) -> Literal["fashion", "beauty"]`
Signature: `keyword_group(keywords, active_window_number) -> tuple[str, str, str, str, str, str]`
Signature: `initial_count_in_window(rows, start, end) -> int`
Signature: `is_initial_candidate_allowed(uploaded_at, collected_at, max_days) -> bool`
```

- [ ] **Step 1: Write failing policy tests**

```python
class SchedulerTests(unittest.TestCase):
    def test_due_time_is_anchored_to_first_snapshot(self) -> None:
        base = datetime(2026, 8, 26, tzinfo=timezone.utc)
        rows = [
            {"url": "https://www.instagram.com/reels/a/", "collection_number": "1", "collected_at": isoformat_utc(base)},
            {"url": "https://www.instagram.com/reels/a/", "collection_number": "2", "collected_at": isoformat_utc(base + timedelta(minutes=30))},
        ]
        jobs = due_jobs(DatasetConfig("fashion", Path("C:/tmp"), FASHION_KEYWORDS), rows, base + timedelta(hours=1))
        self.assertEqual([(job.url, job.due_at) for job in jobs], [("https://www.instagram.com/reels/a/", base + timedelta(hours=1))])

    def test_six_snapshots_produce_no_future_job(self) -> None:
        base = datetime(2026, 8, 26, tzinfo=timezone.utc)
        rows = [{"url": "https://www.instagram.com/reels/a/", "collection_number": str(index + 1), "collected_at": isoformat_utc(base + SNAPSHOT_OFFSETS[index])} for index in range(6)]
        self.assertEqual(due_jobs(DatasetConfig("fashion", Path("C:/tmp"), FASHION_KEYWORDS), rows, base + timedelta(days=2)), [])

    def test_windows_alternate_and_each_keyword_set_has_48_entries(self) -> None:
        base = datetime(2026, 8, 26, tzinfo=timezone.utc)
        self.assertEqual(window_dataset(base, base), "fashion")
        self.assertEqual(window_dataset(base, base + timedelta(minutes=30)), "beauty")
        self.assertEqual(len(FASHION_KEYWORDS), 48)
        self.assertEqual(len(BEAUTY_KEYWORDS), 48)
        self.assertEqual(len(keyword_group(FASHION_KEYWORDS, 7)), 6)

    def test_upload_age_filters_only_initial_candidates(self) -> None:
        captured = datetime(2026, 8, 26, tzinfo=timezone.utc)
        self.assertTrue(is_initial_candidate_allowed("2026-07-27T00:00:00Z", captured, 30))
        self.assertFalse(is_initial_candidate_allowed("2026-07-26T23:59:59Z", captured, 30))
```

- [ ] **Step 2: Run the test module and confirm it fails before implementation**

Run: `& .\.venv\Scripts\python.exe -m unittest collectors.test_fashion_beauty_collection`

Expected: import error for `fashion_beauty_scheduler`.

- [ ] **Step 3: Implement the scheduler module**

```python
SNAPSHOT_OFFSETS = (timedelta(), timedelta(minutes=30), timedelta(hours=1), timedelta(hours=2), timedelta(hours=4), timedelta(hours=8))

def window_dataset(started_at: datetime, now: datetime) -> Literal["fashion", "beauty"]:
    index = int((now - started_at).total_seconds() // (30 * 60))
    return "fashion" if index % 2 == 0 else "beauty"

def due_jobs(dataset: DatasetConfig, rows: list[dict[str, Any]], now: datetime) -> list[DueJob]:
    result: list[DueJob] = []
    for url, snapshots in rows_grouped_by_normalized_url(rows).items():
        snapshots.sort(key=collection_timestamp)
        if len(snapshots) < len(SNAPSHOT_OFFSETS):
            due_at = collection_timestamp(snapshots[0]) + SNAPSHOT_OFFSETS[len(snapshots)]
            if due_at <= now:
                result.append(DueJob(dataset.name, url, due_at))
    return sorted(result, key=lambda job: (job.due_at, job.dataset, job.url))
```

Store exactly 48 keyword strings in each constant, form groups of six by active-window number, and normalize malformed timestamps or URLs out of scheduling decisions.

- [ ] **Step 4: Run the policy tests and verify they pass**

Run: `& .\.venv\Scripts\python.exe -m unittest collectors.test_fashion_beauty_collection.SchedulerTests`

Expected: four passing tests for offsets, final snapshot stop, alternating windows, keyword cardinality, and 30-day eligibility.

### Task 2: Add generic collector controls without changing default behavior

**Files:**
- Modify: `python_version/collectors/instagram_reels_browser.py`
- Modify: `python_version/collectors/test_instagram_reels_browser.py`

**Interfaces:**

```python
Signature: `filter_new_urls(urls: list[str], history_rows: list[dict[str, Any]]) -> list[str]`

class LongReelStore:
    @classmethod
    Signature: `create(csv_path: Path | str, flush_record_count: int = 100, xlsx_layout: str = "columns", disable_recollect_cooldown: bool = False) -> LongReelStore`
```

- [ ] **Step 1: Add failing opt-in behavior tests**

```python
def test_scheduler_flags_are_false_by_default(self) -> None:
    options = parse_args([])
    self.assertFalse(options.new_urls_only)
    self.assertFalse(options.disable_recollect_cooldown)

def test_filter_new_urls_removes_prior_history_url(self) -> None:
    existing = [reel_record(1)]
    urls = [str(existing[0]["url"]), "https://www.instagram.com/reels/new/"]
    self.assertEqual(filter_new_urls(urls, existing), ["https://www.instagram.com/reels/new/"])

async def test_disabled_cooldown_accepts_due_fashion_snapshot(self) -> None:
    store = await LongReelStore.create(self.path, disable_recollect_cooldown=True)
    await store.append(reel_record(1, "2026-08-26T00:00:00Z"))
    result = await store.append(reel_record(1, "2026-08-26T00:30:00Z"))
    self.assertFalse(result.get("skipped"))
```

- [ ] **Step 2: Run the selected collector tests and confirm they fail**

Run: `& .\.venv\Scripts\python.exe -m unittest collectors.test_instagram_reels_browser`

Expected: missing parser attributes, URL filter, and store parameter.

- [ ] **Step 3: Implement the two hidden flags and pass them only to the needed code paths**

```python
parser.add_argument("--new-urls-only", action="store_true", help=argparse.SUPPRESS)
parser.add_argument("--disable-recollect-cooldown", action="store_true", help=argparse.SUPPRESS)

async def append(self, record: dict[str, Any]) -> dict[str, Any]:
    async with self.lock:
        cooldown = None if self.disable_recollect_cooldown else long_collected_record_cooldown(self.rows, record)
        if cooldown:
            return cooldown
```

Use `filter_new_urls` only in the hashtag-discovery branch when `options.new_urls_only` is set. Pass `options.disable_recollect_cooldown` to `LongReelStore.create`. Preserve the current cooldown and candidate behavior for every existing command.

- [ ] **Step 4: Run the complete existing collector suite**

Run: `& .\.venv\Scripts\python.exe -m unittest collectors.test_instagram_reels_browser`

Expected: all existing cooldown, export, and follower tests pass together with the new opt-in tests.

### Task 3: Build the isolated fashion-and-beauty supervisor

**Files:**
- Create: `python_version/collectors/fashion_beauty_collection.py`
- Modify: `python_version/collectors/test_fashion_beauty_collection.py`

**Interfaces:**

```python
Signature: `run_fashion_beauty_collection(config: RunConfig, invoke: Callable, clock: Callable[[], datetime]) -> int`
Signature: `publish_dataset_outputs(dataset: DatasetConfig) -> dict[str, Path]`
Signature: `write_dataset_status(dataset: DatasetConfig, state: dict[str, Any]) -> None`
```

- [ ] **Step 1: Write failing supervisor tests with a fake invocation**

```python
class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_due_job_runs_before_active_window_discovery(self) -> None:
        calls: list[dict[str, object]] = []
        async def fake_invoke(**kwargs: object) -> int:
            calls.append(dict(kwargs))
            return 0
        await run_fashion_beauty_collection(config_with_due_fashion_job, invoke=fake_invoke, clock=clock_at_window_start)
        self.assertEqual(calls[0]["mode"], "recollect")

    async def test_window_cap_500_stops_discovery_but_keeps_due_jobs(self) -> None:
        decision = decide_next_work(config, rows_with_500_initial_fashion_rows, clock_at_fashion_window)
        self.assertFalse(decision.discover)
        self.assertTrue(decision.due_jobs)

    def test_publish_creates_only_domain_named_exports(self) -> None:
        written = publish_dataset_outputs(fashion_dataset_with_fixture_exports)
        self.assertEqual(set(written), {"reels_csv", "reels_json", "reels_xlsx", "users_csv", "users_xlsx"})
        self.assertTrue((data_root / "fashion_reels.xlsx").exists())
        self.assertFalse((data_root / "reels.xlsx").exists())
```

- [ ] **Step 2: Run supervisor tests and confirm they fail**

Run: `& .\.venv\Scripts\python.exe -m unittest collectors.test_fashion_beauty_collection.SupervisorTests`

Expected: import error for `fashion_beauty_collection`.

- [ ] **Step 3: Implement serial invocation, locking, publishing, and status output**

```python
async def run_fashion_beauty_collection(config: RunConfig, *, invoke=invoke_generic_collector, clock=utc_now) -> int:
    started_at = clock()
    discovery_ends_at = started_at + timedelta(hours=config.discovery_hours)
    ends_at = started_at + timedelta(hours=config.duration_hours)
    async with SupervisorLock(config.data_root / ".datasets" / "fashion_beauty_scheduler.lock.json"):
        while clock() < ends_at:
            jobs = sorted(
                (job for dataset in datasets(config) for job in due_jobs(dataset, read_history(dataset), clock())),
                key=lambda job: (job.due_at, job.dataset, job.url),
            )
            next_job = jobs[0] if jobs else None
            if next_job is not None:
                await invoke(config=config, dataset=next_job.dataset, mode="recollect", urls=[next_job.url])
            elif clock() < discovery_ends_at:
                dataset = dataset_by_name(config, window_dataset(started_at, clock()))
                await invoke(config=config, dataset=dataset.name, mode="discover", hashtags=keyword_group(dataset.keywords, active_window_index(started_at, clock())))
            for dataset in datasets(config):
                publish_dataset_outputs(dataset)
                write_dataset_status(dataset, current_status(config, dataset, started_at, discovery_ends_at, ends_at, clock()))
            await wait_for_stop_or_timeout(asyncio.Event(), 30)
    return 0
```

`invoke_generic_collector` builds options with `parse_args` and calls `await run_collector(options)`. It uses the domain workspace as `--data-dir`; discovery sets `--new-urls-only`, `--followers-after-reels`, `--max-upload-age-days 30`, and an item limit equal to the remaining part of that window’s 500 cap. Recollection writes a temporary URL file, sets `--disable-recollect-cooldown`, and deliberately does not set `--max-upload-age-days`. Publish the five generated workspace files with same-directory temporary copies plus `os.replace` to prevent partially written public exports. The supervisor lock is distinct from the generic collector’s short-lived `collector.lock.json`.

- [ ] **Step 4: Run supervisor tests and verify they pass**

Run: `& .\.venv\Scripts\python.exe -m unittest collectors.test_fashion_beauty_collection.SupervisorTests`

Expected: priority, 500-cap, separate-output, and base-output-isolation tests pass without launching Playwright.

### Task 4: Add the `fashion` command and final verification

**Files:**
- Modify: `python_version/scripts/instagram_reels_python.py`
- Modify: `python_version/collector.ps1`
- Modify: `python_version/README.md`
- Modify: `python_version/collectors/test_fashion_beauty_collection.py`

**Interfaces:**

```python
Signature: `parse_fashion_command(arguments: list[str]) -> RunConfig`
```

- [ ] **Step 1: Write failing command parsing tests**

```python
class CommandTests(unittest.TestCase):
    def test_fashion_defaults_match_approved_operation(self) -> None:
        config = parse_fashion_command([])
        self.assertEqual(config.duration_hours, 16)
        self.assertEqual(config.discovery_hours, 8)
        self.assertEqual(config.new_items_per_window, 50)
        self.assertEqual(config.max_new_items_per_window, 500)
        self.assertEqual(config.max_upload_age_days, 30)

    def test_discovery_must_end_eight_hours_before_run_end(self) -> None:
        with self.assertRaises(SystemExit):
            parse_fashion_command(["--duration-hours", "16", "--discovery-hours", "9"])
```

- [ ] **Step 2: Run command tests and confirm they fail**

Run: `& .\.venv\Scripts\python.exe -m unittest collectors.test_fashion_beauty_collection.CommandTests`

Expected: missing `parse_fashion_command`.

- [ ] **Step 3: Implement the launcher and documentation**

```python
if command == "fashion":
    config = parse_fashion_command(arguments)
    return asyncio.run(run_fashion_beauty_collection(config))
```

Accept `--duration-hours`, `--discovery-hours`, `--new-items-per-window`, `--max-new-items-per-window`, `--max-upload-age-days`, `--discovery-interval-minutes`, `--fashion-hashtag-query`, and `--beauty-hashtag-query`. Reject non-finite or non-positive values; reject a cap above 500, a target above the cap, an upload age below zero, or discovery later than `duration_hours - 8`. Add the exact start command to PowerShell examples and explain the two data sets, alternating windows, 30-day initial filter, six snapshots, 50 target/500 cap, output file names, and Ctrl+C graceful stop behavior.

- [ ] **Step 4: Run full tests, help output, and spreadsheet verification**

Run:

```powershell
& .\.venv\Scripts\python.exe -m unittest collectors.test_instagram_reels_browser collectors.test_fashion_beauty_collection exporters.test_instagram_collector
& .\.venv\Scripts\python.exe .\scripts\instagram_reels_python.py --help
```

Expected: all tests pass and help lists `fashion`. Create temporary fixture exports, then use the spreadsheet artifact workflow to inspect `fashion_reels.xlsx`, `fashion_users.xlsx`, `beauty_reels.xlsx`, and `beauty_users.xlsx`; compare headers to the baseline public forms, scan for `#REF!|#DIV/0!|#VALUE!|#NAME?|#N/A`, and render the header plus five rows of every worksheet.

- [ ] **Step 5: Record the verified changes if Git index writing becomes available**

Run:

```powershell
git add -- python_version/collectors/fashion_beauty_scheduler.py python_version/collectors/fashion_beauty_collection.py python_version/collectors/test_fashion_beauty_collection.py python_version/collectors/instagram_reels_browser.py python_version/collectors/test_instagram_reels_browser.py python_version/scripts/instagram_reels_python.py python_version/collector.ps1 python_version/README.md
git commit -m "feat: add fashion and beauty collection scheduler"
```

Expected: commit only the listed implementation files. If the existing `.git/index.lock` permission error recurs, leave the verified files uncommitted and report that environmental limitation without modifying unrelated staged or user files.
