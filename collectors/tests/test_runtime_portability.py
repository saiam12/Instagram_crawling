import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.reels.fashion_beauty_collection import _generic_collector_options
from scripts.instagram_reels_python import parse_scheduled_command


class ScheduledRuntimeTests(unittest.TestCase):
    def test_relative_adb_path_keeps_the_invocation_directory_for_cli_and_environment(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                expected = Path(directory) / "Team SDK" / "platform-tools" / "adb.exe"
                for source in ("cli", "environment"):
                    with self.subTest(source=source), patch.dict(os.environ, {
                        "INSTAGRAM_ANDROID_ADB_PATH": "Team SDK/platform-tools/adb.exe" if source == "environment" else "",
                    }):
                        arguments = ["--android-adb-path", "Team SDK/platform-tools/adb.exe"] if source == "cli" else []
                        config = parse_scheduled_command("fashion", arguments)
                        self.assertEqual(config.android_adb_path, expected)
            finally:
                os.chdir(previous)

    def test_selected_android_runtime_reaches_discovery_and_recollection(self):
        with tempfile.TemporaryDirectory() as directory:
            config = parse_scheduled_command("fashion", [
                "--data-dir", directory, "--android-adb-path", "D:/Team SDK/adb.exe",
                "--android-device-id", "team-phone",
            ])
            self.assertEqual(config.android_adb_path, Path("D:/Team SDK/adb.exe"))
            self.assertEqual(config.android_device_id, "team-phone")
            for mode in ("discover", "recollect"):
                options, urls_file = _generic_collector_options(
                    config=config, dataset="fashion", mode=mode,
                    hashtags=("fashion",), urls=("https://www.instagram.com/reels/ONE/",),
                )
                try:
                    self.assertEqual(options.android_adb_path, config.android_adb_path)
                    self.assertEqual(options.android_device_id, "team-phone")
                finally:
                    if urls_file:
                        urls_file.unlink()

    def test_monitor_selected_device_survives_child_preflight(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "monitor" / "backend"))
        import runner
        identifier = "a" * 32
        job = {"state": "queued", "kind": "collector", "mode": "hybrid", "command": "fashion",
               "device": "team-phone", "args": ["fashion"], "data_root": "unused"}
        observed = []
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(runner, "BASE", Path(directory)), \
             patch.object(runner, "read_json", return_value=job), \
             patch.object(runner, "write_json"), \
             patch.object(runner.subprocess, "run"), \
             patch.dict(os.environ, {"INSTAGRAM_ANDROID_DEVICE_ID": "other-phone"}), \
             patch.object(sys, "argv", ["runner.py", identifier]), \
             patch("scripts.instagram_reels_python.main", side_effect=lambda args: observed.append(os.environ.get("INSTAGRAM_ANDROID_DEVICE_ID")) or 0):
            self.assertEqual(runner.main(), 0)
        self.assertEqual(observed, ["team-phone"])


@unittest.skipUnless(os.name == "nt", "Windows Android launcher")
class AndroidLauncherTests(unittest.TestCase):
    def test_diagnostic_recorder_selects_the_only_device_and_refuses_ambiguity(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "diagnostics" / "watch-android-diagnostics.ps1"
        for scenario in ("single", "multiple"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                adb = root / "fake-adb.ps1"
                adb.write_text("'List of devices attached'; 'team-phone device'; if ($env:RUNTIME_SCENARIO -eq 'multiple') { 'other-phone device' }; $global:LASTEXITCODE = 0", encoding="utf-8")
                harness = r'''
$global:launched = @()
$global:LASTEXITCODE = 0
function Get-Process { param($Id) [pscustomobject]@{Path='mock-powershell.exe'} }
function Start-Process {
    param($FilePath, $ArgumentList, $WindowStyle, [switch]$PassThru, $RedirectStandardOutput, $RedirectStandardError)
    $global:launched = $ArgumentList
    [pscustomobject]@{Id=1234}
}
try {
    & $env:RUNTIME_SCRIPT -AdbPath $env:RUNTIME_ADB -OutputRoot $env:RUNTIME_OUTPUT -Background
    [pscustomobject]@{ok=$true; args=@($global:launched)} | ConvertTo-Json -Depth 3 -Compress
} catch {
    [pscustomobject]@{ok=$false; args=@($global:launched); error=$_.Exception.Message} | ConvertTo-Json -Compress
}
'''
                result = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", harness],
                    env={**os.environ, "RUNTIME_SCRIPT": str(script), "RUNTIME_ADB": str(adb),
                         "RUNTIME_OUTPUT": str(root / "output"), "RUNTIME_SCENARIO": scenario,
                         "INSTAGRAM_ANDROID_DEVICE_ID": ""},
                    capture_output=True, text=True, check=True, timeout=15,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                observed = json.loads(result.stdout.strip().splitlines()[-1])
                if scenario == "single":
                    self.assertTrue(observed["ok"], observed)
                    self.assertEqual(observed["args"][observed["args"].index("-DeviceId") + 1], "team-phone")
                else:
                    self.assertFalse(observed["ok"])
                    self.assertEqual(observed["args"], [])

    def test_avd_graphics_and_booting_headless_process_are_preserved(self):
        source = Path(__file__).resolve().parents[1] / "scripts" / "android" / "start-android.ps1"
        for scenario in ("launch", "headless", "foreground", "online", "ambiguous"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory(prefix="team runtime ") as directory:
                root = Path(directory)
                scripts = root / "scripts" / "android"
                scripts.mkdir(parents=True)
                script = scripts / source.name
                script.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
                adb = root / "fake-adb.ps1"
                adb.write_text(r'''
if ($args[0] -eq 'devices') {
    $global:adbQueries++
    'List of devices attached'
    if ($global:adbQueries -gt 1 -or $env:RUNTIME_SCENARIO -eq 'online') { "emulator-5554`tdevice" }
} elseif ($args -contains 'getprop') { '1' }
elseif ($args -contains 'path') { 'package:/data/app/instagram.apk' }
elseif ($args -contains 'name') { 'TeamAvd'; 'OK' }
$global:LASTEXITCODE = 0
''', encoding="utf-8")
                emulator = root / "fake-emulator.ps1"
                emulator.write_text("'TeamAvd'; 'OtherAvd'; $global:LASTEXITCODE = 0", encoding="utf-8")
                harness = r'''
$global:adbQueries = 0
$global:launches = @()
$global:LASTEXITCODE = 0
function Get-Command { param($Name, $CommandType, $ErrorAction) [pscustomobject]@{Source=$env:RUNTIME_EMULATOR} }
function Get-Process {
    param($Name, $ErrorAction)
    if ($env:RUNTIME_SCENARIO -eq 'headless' -and @($Name) -contains 'qemu-system-x86_64-headless') {
        [pscustomobject]@{WorkingSet64=20MB}
    }
}
function Start-Process {
    param($FilePath, $WindowStyle, $ArgumentList, [switch]$PassThru, $RedirectStandardOutput, $RedirectStandardError)
    $global:launches += [pscustomobject]@{args=$ArgumentList; window=$WindowStyle}
    [pscustomobject]@{Id=1234}
}
try {
    $options = @{AdbPath=$env:RUNTIME_ADB}
    if ($env:RUNTIME_SCENARIO -in @('launch','headless')) { $options.Avd = 'TeamAvd'; $options.NoWindow = $true }
    if ($env:RUNTIME_SCENARIO -eq 'ambiguous') { $options.NoWindow = $true }
    & $env:RUNTIME_SCRIPT @options
    [pscustomobject]@{ok=$true; launches=@($global:launches); device=$env:INSTAGRAM_ANDROID_DEVICE_ID; adb=$env:INSTAGRAM_ANDROID_ADB_PATH} | ConvertTo-Json -Depth 5 -Compress
} catch {
    [pscustomobject]@{ok=$false; error=$_.Exception.Message} | ConvertTo-Json -Compress
}
'''
                env = {**os.environ, "RUNTIME_SCRIPT": str(script), "RUNTIME_ADB": str(adb),
                       "RUNTIME_EMULATOR": str(emulator), "RUNTIME_SCENARIO": scenario,
                       "INSTAGRAM_ANDROID_DEVICE_ID": "", "INSTAGRAM_ANDROID_ADB_PATH": "",
                       "ANDROID_HOME": str(root / "sdk"), "ANDROID_SDK_ROOT": str(root / "sdk"),
                       "LOCALAPPDATA": str(root / "local")}
                result = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", harness],
                    env=env, capture_output=True, text=True, check=True, timeout=25,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                observed = json.loads(result.stdout.strip().splitlines()[-1])
                if scenario == "ambiguous":
                    self.assertFalse(observed["ok"], observed)
                    self.assertIn("choose one with -Avd", observed["error"])
                    continue
                self.assertTrue(observed["ok"], observed)
                self.assertEqual(observed["device"], "emulator-5554")
                self.assertEqual(Path(observed["adb"]), adb)
                launches = observed["launches"]
                if scenario in {"headless", "online"}:
                    self.assertEqual(launches, [])
                else:
                    self.assertEqual(len(launches), 1)
                    self.assertNotIn("-gpu", launches[0]["args"])
                    self.assertNotIn("-wipe-data", launches[0]["args"])
                    self.assertEqual(launches[0]["args"][launches[0]["args"].index("-avd") + 1], "TeamAvd")
                    if scenario == "foreground":
                        self.assertNotIn("-no-window", launches[0]["args"])
                    else:
                        self.assertIn("-no-window", launches[0]["args"])
                    self.assertEqual(launches[0]["window"], "Hidden")
