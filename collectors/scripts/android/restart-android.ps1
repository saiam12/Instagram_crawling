param(
    [Parameter(Mandatory = $true)][string]$DeviceId,
    [Parameter(Mandatory = $true)][string]$EmulatorPath
)

$ErrorActionPreference = 'Stop'
if ($DeviceId -notmatch '^emulator-(\d+)$') {
    throw 'Host restart is restricted to a selected emulator device.'
}
$consolePort = [int]$Matches[1]
$resolvedEmulator = (Resolve-Path -LiteralPath $EmulatorPath).Path
$expectedQemu = @(
    (Join-Path (Split-Path $resolvedEmulator) 'qemu\windows-x86_64\qemu-system-x86_64.exe'),
    (Join-Path (Split-Path $resolvedEmulator) 'qemu\windows-x86_64\qemu-system-x86_64-headless.exe')
)
# Identify only the emulator listening on the selected console port. Refuse
# ambiguous or inaccessible process metadata before terminating anything.
$owners = @(Get-NetTCPConnection -State Listen -LocalPort $consolePort |
    Select-Object -ExpandProperty OwningProcess -Unique)
if ($owners.Count -ne 1) { throw 'Cannot identify a unique emulator process.' }
$target = Get-CimInstance Win32_Process -Filter "ProcessId=$($owners[0])"
if (-not $target -or $target.ExecutablePath -notin $expectedQemu) {
    throw 'Selected port does not belong to the expected SDK emulator.'
}
if ($target.CommandLine -notmatch '(?:^|\s)-avd\s+(?:"([A-Za-z0-9_.-]+)"|([A-Za-z0-9_.-]+))(?=\s|$)') {
    throw 'Cannot identify the selected emulator AVD.'
}
$avdName = if ($Matches[1]) { $Matches[1] } else { $Matches[2] }
Stop-Process -Id $target.ProcessId -Force
$exitDeadline = (Get-Date).AddSeconds(15)
do {
    $remainingProcess = Get-Process -Id $target.ProcessId -ErrorAction SilentlyContinue
    if (-not $remainingProcess -or $remainingProcess.HasExited) { break }
    if ((Get-Date) -ge $exitDeadline) {
        throw 'Selected emulator did not exit; refusing to launch a duplicate.'
    }
    Start-Sleep -Milliseconds 250
} while ($true)
# Cold boot the same AVD and port; retain installed apps, login and user data.
Start-Process -FilePath $resolvedEmulator -WindowStyle Hidden -ArgumentList @(
    '-avd', $avdName, '-port', "$consolePort", '-no-snapshot', '-no-window'
) | Out-Null
