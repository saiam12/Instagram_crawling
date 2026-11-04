from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == "nt", "Windows host restart helper")
class AndroidHostRestartTests(unittest.TestCase):
    def test_host_restart_targets_only_verified_emulator_and_preserves_data(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "android" / "restart-android.ps1"
        for scenario in ("valid", "headless", "delayed_exit", "already_exited", "wrong_process", "multiple_owners", "invalid_avd"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                emulator = Path(directory) / "emulator.exe"
                emulator.touch()
                harness = r'''
$global:actions = @()
$global:exitChecks = 0
function Get-NetTCPConnection {
    param($State, $LocalPort)
    if ($LocalPort -ne 5554) { throw 'Wrong port' }
    [pscustomobject]@{OwningProcess=1234}
    if ($env:RESTART_TEST_SCENARIO -eq 'multiple_owners') {
        [pscustomobject]@{OwningProcess=5678}
    }
}
function Get-CimInstance {
    param($ClassName, $Filter)
    if ($Filter -ne 'ProcessId=1234') { throw 'Wrong PID filter' }
    $path = Join-Path (Split-Path $env:RESTART_TEST_EMULATOR) 'qemu\windows-x86_64\qemu-system-x86_64.exe'
    if ($env:RESTART_TEST_SCENARIO -eq 'headless') {
        $path = Join-Path (Split-Path $env:RESTART_TEST_EMULATOR) 'qemu\windows-x86_64\qemu-system-x86_64-headless.exe'
    }
    if ($env:RESTART_TEST_SCENARIO -eq 'wrong_process') { $path = 'C:\unrelated.exe' }
    $command = '-avd Pixel_8_clean'
    if ($env:RESTART_TEST_SCENARIO -eq 'invalid_avd') { $command = '-avd "bad name"' }
    [pscustomobject]@{ProcessId=1234; ExecutablePath=$path; CommandLine=$command}
}
function Stop-Process { param($Id, [switch]$Force) $global:actions += "stop:$Id" }
function Wait-Process { param($Id, $Timeout, $ErrorAction) }
function Get-Process {
    param($Id, $ErrorAction)
    $global:exitChecks += 1
    if ($env:RESTART_TEST_SCENARIO -eq 'already_exited') {
        [pscustomobject]@{HasExited=$true}
    }
    if ($env:RESTART_TEST_SCENARIO -eq 'delayed_exit' -and $global:exitChecks -le 2) {
        [pscustomobject]@{HasExited=$false}
    }
}
function Start-Process {
    param($FilePath, $WindowStyle, $ArgumentList)
    $global:actions += [pscustomobject]@{path=$FilePath; window=$WindowStyle; args=$ArgumentList}
}
try {
    & $env:RESTART_TEST_SCRIPT -DeviceId emulator-5554 -EmulatorPath $env:RESTART_TEST_EMULATOR
    [pscustomobject]@{ok=$true; actions=@($global:actions)} | ConvertTo-Json -Depth 5 -Compress
} catch {
    [pscustomobject]@{ok=$false; actions=@($global:actions); error=$_.Exception.Message} | ConvertTo-Json -Depth 5 -Compress
}
'''
                result = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", harness],
                    env={**os.environ, "RESTART_TEST_SCENARIO": scenario,
                         "RESTART_TEST_EMULATOR": str(emulator), "RESTART_TEST_SCRIPT": str(script)},
                    capture_output=True, text=True, check=True, timeout=20,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                observed = json.loads(result.stdout)
                if scenario not in {"valid", "headless", "delayed_exit", "already_exited"}:
                    self.assertFalse(observed["ok"])
                    self.assertEqual(observed["actions"], [])
                    continue
                self.assertTrue(observed["ok"], observed)
                self.assertEqual(observed["actions"][0], "stop:1234")
                launch = observed["actions"][1]
                self.assertEqual(launch["window"], "Hidden")
                self.assertEqual(launch["args"], ["-avd", "Pixel_8_clean", "-port", "5554", "-no-snapshot", "-no-window"])
