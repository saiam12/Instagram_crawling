"""Exercise first-run launchers with a local wheel, without network or login."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile


COLLECTORS = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Windows PowerShell launchers")
class AutoSetupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="collector setup ")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for relative in ("collector.ps1", "requirements.txt", "scripts/setup/repair_venv.ps1",
                         "monitor/start.ps1"):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(COLLECTORS / relative, target)
        (self.root / "scripts/instagram_reels_python.py").write_text(
            "import json,os,sys\nprint('LAUNCHED='+json.dumps(sys.argv[1:]))\n"
            "sys.exit(int(os.environ.get('COLLECTOR_TEST_EXIT', '0')))\n", encoding="utf-8")
        wheels = self.root / "wheels"
        wheels.mkdir()
        with zipfile.ZipFile(wheels / "playwright-1.99.0-py3-none-any.whl", "w") as wheel:
            for name, content in {
                "playwright/__init__.py": "",
                "playwright/sync_api.py": "",
                "playwright/async_api.py": "",
                "playwright-1.99.0.dist-info/METADATA":
                    "Metadata-Version: 2.1\nName: playwright\nVersion: 1.99.0\n",
                "playwright-1.99.0.dist-info/WHEEL":
                    "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
                "playwright-1.99.0.dist-info/RECORD": "",
            }.items():
                wheel.writestr(name, content)
        self.env = {**os.environ, "PIP_NO_INDEX": "1", "PIP_FIND_LINKS": wheels.as_uri(),
                    "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONPATH": "unrelated-path",
                    "COLLECTOR_TEST_ROOT": str(self.root)}
        # Let Windows PowerShell load its own modules when tests run under pwsh.
        self.env.pop("PSMODULEPATH", None)

    def run_shell(self, *arguments):
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", *arguments],
            cwd=self.root.parent, env=self.env, capture_output=True, text=True, timeout=90,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def collect(self, *arguments):
        return self.run_shell("-File", str(self.root / "collector.ps1"), *arguments)

    def assert_started(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("LAUNCHED=", result.stdout)

    def test_first_launch_installs_then_reuses_environment_and_preserves_local_data(self):
        for relative in ("data/collector/reels.csv", "runtime/browser_profile/keep.txt"):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"preserve")
        self.assert_started(self.collect("--collector-mode", "web", "--max-items", "1"))
        self.assertTrue((self.root / ".venv/Scripts/python.exe").is_file())
        stamp = self.root / ".venv/.requirements.sha256"
        before = stamp.stat().st_mtime_ns
        self.assert_started(self.collect("--help"))
        self.assertEqual(stamp.stat().st_mtime_ns, before)
        previous_hash = stamp.read_bytes()
        with (self.root / "requirements.txt").open("a") as requirements:
            requirements.write("\n# Updated after git pull\n")
        self.assert_started(self.collect("--help"))
        self.assertNotEqual(stamp.read_bytes(), previous_hash)
        current_hash = stamp.read_bytes()
        stamp.write_bytes(b"")
        self.assert_started(self.collect("--help"))
        self.assertEqual(stamp.read_bytes(), current_hash)
        for relative in ("data/collector/reels.csv", "runtime/browser_profile/keep.txt"):
            self.assertEqual((self.root / relative).read_bytes(), b"preserve")
        self.env["COLLECTOR_TEST_EXIT"] = "7"
        self.assertEqual(self.collect("--help").returncode, 7)

    def test_missing_python_stops_with_install_instructions(self):
        result = self.run_shell("-Command", r'''
function Get-Command { param($Name, $CommandType, $ErrorAction) return $null }
& (Join-Path $env:COLLECTOR_TEST_ROOT 'collector.ps1') --help
''')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Python 3.12+ not found", result.stderr)
        self.assertNotIn("LAUNCHED=", result.stdout)
        self.assertFalse((self.root / ".venv").exists())

    def test_requirement_update_failure_stops_launch_and_does_not_mark_setup_complete(self):
        self.assert_started(self.collect("--help"))
        stamp = self.root / ".venv/.requirements.sha256"
        before = stamp.read_bytes()
        requirements = self.root / "requirements.txt"
        requirements.write_text(requirements.read_text() + "\nmissing-collector-test-package==0\n")
        failed = self.collect("--help")
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertNotIn("LAUNCHED=", failed.stdout)
        self.assertEqual(stamp.read_bytes(), before)
        shutil.copyfile(COLLECTORS / "requirements.txt", requirements)
        self.assert_started(self.collect("--help"))
        requirements.unlink()
        failed = self.collect("--help")
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertNotIn("LAUNCHED=", failed.stdout)

    def test_missing_package_is_repaired_even_when_requirements_have_not_changed(self):
        self.assert_started(self.collect("--help"))
        package = self.root / ".venv/Lib/site-packages/playwright"
        shutil.rmtree(package)
        self.assert_started(self.collect("--help"))
        self.assertTrue((package / "async_api.py").is_file())

    def test_monitor_first_launch_prepares_environment(self):
        result = self.run_shell("-Command", r'''
function Invoke-RestMethod {
    [pscustomobject]@{app='collector-monitor'; data_root=(Join-Path $env:COLLECTOR_TEST_ROOT 'data\collector')}
}
& (Join-Path $env:COLLECTOR_TEST_ROOT 'monitor\start.ps1') -NoBrowser
''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.root / ".venv/Scripts/python.exe").is_file())
        self.assertIn("Collector Monitor:", result.stdout)

    def test_hybrid_preparation_uses_project_module_from_another_directory(self):
        android = self.root / "scripts/android/start-android.ps1"
        android.parent.mkdir(parents=True)
        android.write_text("$global:LASTEXITCODE = 0\n")
        module = self.root / "src/reels/android_uiautomator.py"
        module.parent.mkdir(parents=True)
        module.write_text("print('ANDROID_PREPARED')\n")
        result = self.collect("--collector-mode", "hybrid", "--max-items", "1")
        self.assert_started(result)
        self.assertIn("ANDROID_PREPARED", result.stdout)


if __name__ == "__main__":
    unittest.main()
