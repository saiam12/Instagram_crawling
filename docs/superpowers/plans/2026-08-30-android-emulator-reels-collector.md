# Android Emulator Reels Collector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (\`- [ ]\`) syntax for tracking.

**Goal:** Build an isolated Android Studio emulator collector that reads visible Instagram Reel information through ADB/UIAutomator for feed, hashtag, and supplied-URL workflows.

**Architecture:** android_emulator_version owns a synchronous Python package. A narrow AndroidDriver protocol isolates interaction from parsing, workflow, and export code; AdbDriver is its first implementation. The parser accepts only fully displayed integer values and preserves original UI XML, PNG, and text for every observation.

**Tech Stack:** Python 3.12+, Android SDK Platform Tools (adb), UIAutomator XML, openpyxl, standard-library unittest.

**Spec:** docs/superpowers/specs/2026-08-30-android-emulator-reels-collector-design.md

## Global Constraints

- Create only android_emulator_version source and output paths; never import, edit, lock, or overwrite the existing web collector versions.
- Use the user's authenticated emulator session; no code may like, follow, comment, post, send, or bypass login, challenge, CAPTCHA, or rate-limit screens.
- Use exact visible non-negative integers only. Preserve compact and unrecognised text without inferring a number.
- Preserve an XML and PNG evidence pair for every observed Reel, including layout and access failures.
- Tests must use fake drivers and XML fixtures only; they may not open Instagram or require an attached emulator.
- Public exports are data_android/reels.csv, data_android/reels.json, and data_android/reels.xlsx. Replace each only after its complete temporary file has been written.

---

## File Structure

~~~
android_emulator_version/
├── README.md
├── requirements.txt
├── collector.ps1
├── collect_android_reels.py
├── android_collector/
│   ├── __init__.py
│   ├── models.py
│   ├── driver.py
│   ├── adb_driver.py
│   ├── ui_parser.py
│   ├── store.py
│   └── workflows.py
└── tests/
    ├── fixtures/reel_visible.xml
    ├── test_ui_parser.py
    ├── test_adb_driver.py
    ├── test_store.py
    ├── test_workflows.py
    └── test_cli.py
~~~

### Task 1: Data model and UI parser

**Files:**
- Create: android_emulator_version/android_collector/__init__.py
- Create: android_emulator_version/android_collector/models.py
- Create: android_emulator_version/android_collector/ui_parser.py
- Create: android_emulator_version/tests/fixtures/reel_visible.xml
- Create: android_emulator_version/tests/test_ui_parser.py

**Interfaces:**
- Produces Metric(label: str, value: int | None, raw_text: str), EvidencePaths(xml_path: str, png_path: str), and ObservedReel from models.py.
- Produces parse_exact_count(text: str) -> int | None, detect_access_block(xml: str) -> str | None, parse_ui_xml(xml: str) -> list[UiNode], and parse_visible_reel(xml: str, source_mode: str, source_query: str, reel_url: str, collected_at: str) -> ObservedReel.

- [ ] **Step 1: Write the failing parser tests**

~~~python
def test_parse_exact_count_accepts_full_visible_integer_only(self) -> None:
    self.assertEqual(parse_exact_count("32,357"), 32357)
    self.assertEqual(parse_exact_count("4 699"), 4699)
    self.assertIsNone(parse_exact_count("5.7K"))
    self.assertIsNone(parse_exact_count("좋아요 4,699"))

def test_detect_access_block_recognises_login_and_rate_limit(self) -> None:
    self.assertEqual(detect_access_block('<node text="Log in to continue"/>'), "login_required")
    self.assertEqual(detect_access_block('<node text="잠시 후 다시 시도하세요"/>'), "rate_limited")

def test_parse_visible_reel_preserves_likes_and_plays_label(self) -> None:
    observed = parse_visible_reel(FIXTURE_XML, "feed", "", "", "2026-08-30T00:00:00Z")
    self.assertEqual(observed.username, "odi.pigi")
    self.assertEqual(observed.metrics["likes_and_plays_count"].value, 32357)
    self.assertEqual(observed.metrics["likes_and_plays_count"].raw_text, "32,357")
~~~

- [ ] **Step 2: Run the parser tests and verify red**

Run: cd android_emulator_version; python -m unittest tests.test_ui_parser -v

Expected: ModuleNotFoundError for android_collector or an ImportError for a missing parser symbol.

- [ ] **Step 3: Implement the minimal model and parser**

~~~python
def parse_exact_count(text: str) -> int | None:
    candidate = re.sub(r"[\s,]", "", text.strip())
    return int(candidate) if re.fullmatch(r"\d+", candidate) else None

def build_fingerprint(username: str, caption: str, audio_name: str) -> str:
    value = "\x1f".join(item.strip().casefold() for item in (username, caption, audio_name))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
~~~

Use ElementTree to retain each node's text, content-desc, resource-id, and bounds. Map labels for likes, comments, shares, reposts, saves, and the literal Likes and plays panel; store unmatched value-bearing labels in visible_metrics_json.

- [ ] **Step 4: Run parser tests and verify green**

Run: cd android_emulator_version; python -m unittest tests.test_ui_parser -v

Expected: all parser tests pass.

- [ ] **Step 5: Commit the parser task**

~~~powershell
git add android_emulator_version/android_collector android_emulator_version/tests/fixtures android_emulator_version/tests/test_ui_parser.py
git commit -m "feat: parse visible Android Reel metrics"
~~~

### Task 2: Driver protocol and ADB implementation

**Files:**
- Create: android_emulator_version/android_collector/driver.py
- Create: android_emulator_version/android_collector/adb_driver.py
- Create: android_emulator_version/tests/test_adb_driver.py

**Interfaces:**
- Consumes: models from Task 1.
- Produces AndroidDriver protocol methods ensure_ready(), launch_instagram(), dump_ui(), tap_text(labels), input_text(value), swipe_up(), open_reel_url(url), and capture_screenshot(path).
- Produces select_online_device(adb_path: Path, device_id: str | None, adb_user_home: Path, runner: Callable) -> str and AdbDriver.

- [ ] **Step 1: Write failing driver tests with a fake subprocess runner**

~~~python
def test_select_online_device_returns_the_only_emulator(self) -> None:
    runner = FakeRunner("List of devices attached\nemulator-5554\tdevice product:sdk\n")
    actual = select_online_device(Path("adb"), None, Path(".adb-user"), runner)
    self.assertEqual(actual, "emulator-5554")

def test_select_online_device_requires_device_id_for_multiple_devices(self) -> None:
    runner = FakeRunner("List of devices attached\nemulator-5554\tdevice\nemulator-5556\tdevice\n")
    with self.assertRaisesRegex(RuntimeError, "--device-id"):
        select_online_device(Path("adb"), None, Path(".adb-user"), runner)

def test_open_reel_url_uses_an_android_view_intent(self) -> None:
    self.driver.open_reel_url("https://www.instagram.com/reel/ABC/")
    self.assertIn(["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", "https://www.instagram.com/reel/ABC/"], self.runner.arguments)
~~~

- [ ] **Step 2: Run driver tests and verify red**

Run: cd android_emulator_version; python -m unittest tests.test_adb_driver -v

Expected: ModuleNotFoundError for android_collector.adb_driver.

- [ ] **Step 3: Implement protocol, device selection, and bounded ADB commands**

~~~python
class AndroidDriver(Protocol):
    def ensure_ready(self) -> None: ...
    def launch_instagram(self) -> None: ...
    def dump_ui(self) -> str: ...
    def tap_text(self, labels: Sequence[str]) -> bool: ...
    def input_text(self, value: str) -> None: ...
    def swipe_up(self) -> None: ...
    def open_reel_url(self, url: str) -> None: ...
    def capture_screenshot(self, path: Path) -> None: ...

def _adb_environment(adb_user_home: Path) -> dict[str, str]:
    return {**os.environ, "ANDROID_USER_HOME": str(adb_user_home)}
~~~

Make dump_ui run uiautomator dump to /sdcard/window.xml and then adb exec-out cat that path. Make tap_text compute a node center from UI bounds. All commands must be from the allowed interaction list; tests must assert no invocation contains like, follow, comment, send, or post.

- [ ] **Step 4: Run driver tests and verify green**

Run: cd android_emulator_version; python -m unittest tests.test_adb_driver -v

Expected: all driver tests pass with the fake runner only.

- [ ] **Step 5: Commit the driver task**

~~~powershell
git add android_emulator_version/android_collector/driver.py android_emulator_version/android_collector/adb_driver.py android_emulator_version/tests/test_adb_driver.py
git commit -m "feat: add isolated Android ADB driver"
~~~

### Task 3: Evidence persistence and public exports

**Files:**
- Create: android_emulator_version/android_collector/store.py
- Create: android_emulator_version/tests/test_store.py
- Create: android_emulator_version/requirements.txt

**Interfaces:**
- Consumes: AndroidDriver, EvidencePaths, and ObservedReel.
- Produces CollectionStore(data_dir: Path), save_evidence(index: int, xml: str, driver: AndroidDriver) -> EvidencePaths, append(observation: ObservedReel) -> None, export() -> None, and read_reel_urls_from_xlsx(path: Path) -> list[str].

- [ ] **Step 1: Write failing export tests**

~~~python
def test_export_writes_matching_csv_json_and_xlsx(self) -> None:
    store = CollectionStore(self.data_dir)
    evidence = store.save_evidence(1, "<hierarchy/>", FakeDriver())
    store.append(observed_reel(evidence_paths=evidence, likes_and_plays=32357, raw_likes_and_plays="32,357"))
    store.export()
    self.assertEqual(read_csv(self.data_dir / "reels.csv")[0]["likes_and_plays_count"], "32357")
    self.assertEqual(json.loads((self.data_dir / "reels.json").read_text())[0]["likes_and_plays_count_raw"], "32,357")
    self.assertEqual(load_workbook(self.data_dir / "reels.xlsx").active.max_row, 2)

def test_xlsx_url_reader_rejects_non_instagram_rows(self) -> None:
    write_workbook(self.data_dir / "urls.xlsx", ["url"], ["https://www.instagram.com/reel/ABC/"], ["https://example.com/"])
    self.assertEqual(read_reel_urls_from_xlsx(self.data_dir / "urls.xlsx"), ["https://www.instagram.com/reel/ABC/"])
~~~

- [ ] **Step 2: Run store tests and verify red**

Run: cd android_emulator_version; python -m unittest tests.test_store -v

Expected: ModuleNotFoundError for android_collector.store.

- [ ] **Step 3: Implement evidence and atomic exporters**

~~~text
openpyxl>=3.1,<4
~~~

~~~python
def _replace_atomically(destination: Path, write: Callable[[Path], None]) -> None:
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    write(temporary)
    temporary.replace(destination)

def save_evidence(self, index: int, xml: str, driver: AndroidDriver) -> EvidencePaths:
    stem = self.evidence_dir / f"{index:06d}"
    xml_path = stem.with_suffix(".xml")
    xml_path.write_text(xml, encoding="utf-8")
    png_path = stem.with_suffix(".png")
    driver.capture_screenshot(png_path)
    return EvidencePaths(xml_path, png_path)
~~~

Use openpyxl to write reels.xlsx. Keep an empty reel_url for feed/hashtag discoveries and retain source_mode, source_query, fingerprint, raw metric columns, and visible_metrics_json.

- [ ] **Step 4: Run store, parser, and driver tests**

Run: cd android_emulator_version; python -m unittest tests.test_store tests.test_ui_parser tests.test_adb_driver -v

Expected: all three modules pass.

- [ ] **Step 5: Commit the persistence task**

~~~powershell
git add android_emulator_version/android_collector/store.py android_emulator_version/requirements.txt android_emulator_version/tests/test_store.py
git commit -m "feat: export Android Reel observations with evidence"
~~~

### Task 4: Read-only feed, hashtag, and URL workflows

**Files:**
- Create: android_emulator_version/android_collector/workflows.py
- Create: android_emulator_version/tests/test_workflows.py

**Interfaces:**
- Consumes: AndroidDriver, parser APIs, and CollectionStore.
- Produces CollectorOptions, preflight(driver: AndroidDriver) -> None, run_feed(options, driver, store) -> int, run_hashtag(options, driver, store) -> int, and run_refresh(options, driver, store, urls) -> int.

- [ ] **Step 1: Write failing workflow tests using a fake driver**

~~~python
def test_run_feed_stops_when_a_reel_fingerprint_repeats(self) -> None:
    driver = FakeDriver(ui_xml=[REEL_XML, REEL_XML])
    stored = run_feed(CollectorOptions(max_items=10, delay_seconds=0), driver, self.store)
    self.assertEqual(stored, 1)
    self.assertEqual(driver.swipe_count, 1)
    self.assertFalse(driver.mutating_operation_called)

def test_run_refresh_opens_each_supplied_url(self) -> None:
    urls = ["https://www.instagram.com/reel/ABC/", "https://www.instagram.com/reel/DEF/"]
    self.assertEqual(run_refresh(CollectorOptions(max_items=2, delay_seconds=0), self.driver, self.store, urls), 2)
    self.assertEqual(self.driver.opened_urls, urls)

def test_preflight_stops_at_a_login_wall(self) -> None:
    with self.assertRaisesRegex(AccessBlockedError, "login_required"):
        preflight(FakeDriver(ui_xml=['<node text="Log in to continue"/>']))
~~~

- [ ] **Step 2: Run workflow tests and verify red**

Run: cd android_emulator_version; python -m unittest tests.test_workflows -v

Expected: ModuleNotFoundError for android_collector.workflows.

- [ ] **Step 3: Implement preflight and a single evidence-backed observation**

~~~python
def preflight(driver: AndroidDriver) -> None:
    driver.ensure_ready()
    blocked = detect_access_block(driver.dump_ui())
    if blocked:
        raise AccessBlockedError(blocked)

def collect_current_reel(options: CollectorOptions, driver: AndroidDriver, store: CollectionStore) -> ObservedReel:
    xml = driver.dump_ui()
    evidence = store.save_evidence(store.next_index(), xml, driver)
    observed = parse_visible_reel(xml, options.source_mode, options.source_query, options.reel_url, utc_now_iso())
    store.append(observed.with_evidence(evidence))
    return observed
~~~

- [ ] **Step 4: Extend tests for hashtag search and an access error**

~~~python
def test_run_hashtag_enters_each_query_and_collects_visible_reels(self) -> None:
    result = run_hashtag(CollectorOptions(hashtags=("패션", "ootd"), max_items=2, delay_seconds=0), self.driver, self.store)
    self.assertEqual(result, 2)
    self.assertEqual(self.driver.entered_text, ["패션", "ootd"])
~~~

- [ ] **Step 5: Implement the bounded loops**

Feed must launch Instagram, tap Reels or 릴스, observe, and swipe after a new fingerprint. Hashtag mode must tap Search or 검색, enter one hashtag at a time, tap the Reels or 릴스 result surface, then use the same observation loop. Refresh must deep-link each URL, observe it, and retain that exact URL. Each loop checks detect_access_block after every dump, ends at max_items, and calls store.export on normal exit.

- [ ] **Step 6: Run the complete test suite**

Run: cd android_emulator_version; python -m unittest discover -s tests -v

Expected: all fake-driver tests pass without ADB or Instagram.

- [ ] **Step 7: Commit the workflow task**

~~~powershell
git add android_emulator_version/android_collector/workflows.py android_emulator_version/tests/test_workflows.py
git commit -m "feat: automate read-only Android Reel workflows"
~~~

### Task 5: CLI, PowerShell launcher, documentation, and end-to-end verification

**Files:**
- Create: android_emulator_version/collect_android_reels.py
- Create: android_emulator_version/collector.ps1
- Create: android_emulator_version/README.md
- Create: android_emulator_version/tests/test_cli.py
- Modify: README.md

**Interfaces:**
- Consumes: AdbDriver, CollectionStore, CollectorOptions, and all workflow entry points.
- Produces main(argv: Sequence[str] | None = None) -> int.

- [ ] **Step 1: Write failing CLI tests**

~~~python
@patch("collect_android_reels.run_hashtag")
@patch("collect_android_reels.create_driver")
def test_hashtag_command_splits_or_query_and_dispatches(self, create_driver, run_hashtag) -> None:
    self.assertEqual(main(["hashtag", "--hashtag", "패션 OR ootd", "--max-items", "2"]), 0)
    run_hashtag.assert_called_once()

def test_refresh_requires_an_existing_xlsx_file(self) -> None:
    with self.assertRaises(SystemExit):
        parse_args(["refresh", "--urls-file", "missing.xlsx"])
~~~

- [ ] **Step 2: Run CLI tests and verify red**

Run: cd android_emulator_version; python -m unittest tests.test_cli -v

Expected: ModuleNotFoundError for collect_android_reels.

- [ ] **Step 3: Implement command parsing and dispatch**

~~~python
def main(argv: Sequence[str] | None = None) -> int:
    options = parse_args(argv)
    driver = create_driver(options.adb_path, options.device_id, options.adb_user_home)
    store = CollectionStore(options.data_dir)
    if options.command == "feed":
        return run_feed(options, driver, store)
    if options.command == "hashtag":
        return run_hashtag(options, driver, store)
    urls = read_reel_urls_from_xlsx(options.urls_file)
    return run_refresh(options, driver, store, urls)
~~~

The README and PowerShell launcher must document the installation command, all three modes, --device-id, manual login/challenge handling, evidence locations, and the rule against account-mutating actions.

- [ ] **Step 4: Run fresh final verification**

Run: cd android_emulator_version; python -m unittest discover -s tests -v; python -m compileall android_collector collect_android_reels.py; python collect_android_reels.py --help

Expected: zero test failures, successful compilation, and feed, hashtag, and refresh shown in help.

- [ ] **Step 5: Verify feature isolation and whitespace**

Run: git diff --check; git status --short; git diff -- README.md android_emulator_version

Expected: this feature changes only README.md and android_emulator_version; previously modified web-collector files remain untouched.

- [ ] **Step 6: Commit the CLI and documentation task**

~~~powershell
git add README.md android_emulator_version
git commit -m "feat: add Android emulator Instagram collector"
~~~
