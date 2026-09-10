param(
    [string]$Avd = 'Pixel_8_clean',
    [switch]$NoWindow
)

$ErrorActionPreference = 'Stop'
$emulatorPath = Join-Path $env:LOCALAPPDATA 'Android\Sdk\emulator\emulator.exe'
$adbPath = Join-Path $env:LOCALAPPDATA 'Android\Sdk\platform-tools\adb.exe'
if (-not (Test-Path -LiteralPath $emulatorPath)) {
    throw "Android Emulator not found: $emulatorPath"
}
if (-not (Test-Path -LiteralPath $adbPath)) {
    throw "ADB not found: $adbPath"
}
if ($Avd -notmatch '^[A-Za-z0-9_-]+$') {
    throw 'AVD name must contain only letters, numbers, underscores or hyphens.'
}

function Get-OnlineAndroidDevice {
    # On its first invocation ADB writes the normal daemon-start banner to
    # stderr even when `adb devices` succeeds. With ErrorActionPreference=Stop,
    # Windows PowerShell turns that banner into a terminating NativeCommandError.
    # Capture both streams for this command and use its exit code to distinguish
    # a real ADB failure from successful daemon startup.
    $savedErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $adbOutput = @(& $adbPath devices 2>&1)
        $adbExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $savedErrorActionPreference
    }
    if ($adbExitCode -ne 0) {
        $adbError = ($adbOutput | Out-String).Trim()
        throw "ADB device query failed (exit $adbExitCode): $adbError"
    }
    $deviceLines = @($adbOutput |
        ForEach-Object { $_.ToString() } |
        Where-Object { $_ -match '^\S+\s+device$' })
    $deviceLine = $deviceLines |
        Where-Object { $_ -match '^emulator-\d+\s+device$' } |
        Select-Object -First 1
    if (-not $deviceLine) {
        $deviceLine = $deviceLines | Select-Object -First 1
    }
    if ($deviceLine) {
        return ($deviceLine -split '\s+')[0]
    }
    return $null
}

$device = Get-OnlineAndroidDevice
if (-not $device) {
    $runningEmulator = Get-Process qemu-system-x86_64 -ErrorAction SilentlyContinue |
        Where-Object { $_.WorkingSet64 -gt 10MB } |
        Select-Object -First 1
    if (-not $runningEmulator) {
        # Preserve login/user data. Never pass -wipe-data when restarting this AVD.
        $emulatorArguments = @(
            '-avd', $Avd,
            '-gpu', 'host',
            '-feature', '-Vulkan',
            '-no-snapshot',
            '-no-audio'
        )
        if ($NoWindow) {
            $emulatorArguments += '-no-window'
        }
        Start-Process -FilePath $emulatorPath -ArgumentList $emulatorArguments | Out-Null
        $displayMode = if ($NoWindow) { 'background' } else { 'foreground' }
        Write-Host "Android emulator started: $Avd ($displayMode)"
    }
}

$deadline = (Get-Date).AddMinutes(3)
$bootCompleted = ''
do {
    $device = Get-OnlineAndroidDevice
    if ($device) {
        $bootCompleted = (& $adbPath -s $device shell getprop sys.boot_completed 2>$null | Out-String).Trim()
    }
    if ($bootCompleted -ne '1') {
        Start-Sleep -Seconds 2
    }
} while ($bootCompleted -ne '1' -and (Get-Date) -lt $deadline)

if (-not $device -or $bootCompleted -ne '1') {
    throw "Android emulator did not finish booting within 3 minutes: $Avd"
}

$instagramPackage = (& $adbPath -s $device shell pm path com.instagram.android 2>$null | Out-String).Trim()
if (-not $instagramPackage) {
    throw "Instagram is not installed on Android device $device."
}
$env:INSTAGRAM_ANDROID_DEVICE_ID = $device
$avdName = (& $adbPath -s $device emu avd name 2>$null | Where-Object { $_ -and $_ -ne 'OK' } | Select-Object -First 1)
if ($avdName) {
    Write-Host "Android ready: $device (AVD: $avdName)"
}
else {
    Write-Host "Android ready: $device"
}
