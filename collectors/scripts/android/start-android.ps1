param(
    [string]$Avd = $env:INSTAGRAM_ANDROID_AVD,
    [string]$AdbPath = $env:INSTAGRAM_ANDROID_ADB_PATH,
    [string]$DeviceId = $env:INSTAGRAM_ANDROID_DEVICE_ID,
    [switch]$NoWindow
)

$ErrorActionPreference = 'Stop'
$sdkRoots = @($env:ANDROID_HOME, $env:ANDROID_SDK_ROOT)
if ($env:LOCALAPPDATA) { $sdkRoots += (Join-Path $env:LOCALAPPDATA 'Android\Sdk') }
if (-not $AdbPath) {
    foreach ($sdkRoot in $sdkRoots) {
        if (-not $sdkRoot) { continue }
        $candidate = Join-Path $sdkRoot 'platform-tools\adb.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { $AdbPath = $candidate; break }
    }
    if (-not $AdbPath) {
        $adbCommand = Get-Command adb.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($adbCommand) { $AdbPath = $adbCommand.Source }
    }
}
if (-not $AdbPath -or -not (Test-Path -LiteralPath $AdbPath -PathType Leaf)) {
    throw 'ADB not found. Install Android SDK Platform-Tools and set ANDROID_HOME, or pass --android-adb-path to collector.ps1.'
}
$adbPath = (Resolve-Path -LiteralPath $AdbPath).Path
# The Python process and detached workers must use the same SDK as preflight.
$env:INSTAGRAM_ANDROID_ADB_PATH = $adbPath
if ($Avd -and $Avd -notmatch '^[A-Za-z0-9_.-]+$') {
    throw 'AVD name must contain only letters, numbers, underscores, dots or hyphens.'
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
    if ($DeviceId) {
        return ($deviceLines | ForEach-Object { ($_ -split '\s+')[0] } |
            Where-Object { $_ -eq $DeviceId } | Select-Object -First 1)
    }
    if ($deviceLines.Count -gt 1) {
        throw 'Multiple Android devices are online. Pass --android-device-id SERIAL to collector.ps1.'
    }
    if ($deviceLines.Count -eq 1) {
        return ($deviceLines[0] -split '\s+')[0]
    }
    return $null
}

$device = Get-OnlineAndroidDevice
if ($DeviceId -and -not $device) {
    throw "Selected Android device is not online: $DeviceId. Start it and authorize USB debugging before retrying."
}
if (-not $device) {
    $runningEmulator = Get-Process -Name qemu-system-x86_64, qemu-system-x86_64-headless -ErrorAction SilentlyContinue |
        Where-Object { $_.WorkingSet64 -gt 10MB } |
        Select-Object -First 1
    if (-not $runningEmulator) {
        $emulatorPath = $null
        $emulatorRoots = @((Split-Path (Split-Path $adbPath))) + $sdkRoots
        foreach ($sdkRoot in $emulatorRoots) {
            if (-not $sdkRoot) { continue }
            $candidate = Join-Path $sdkRoot 'emulator\emulator.exe'
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { $emulatorPath = $candidate; break }
        }
        if (-not $emulatorPath) {
            $emulatorCommand = Get-Command emulator.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($emulatorCommand) { $emulatorPath = $emulatorCommand.Source }
        }
        if (-not $emulatorPath) {
            throw 'Android Emulator not found. Install it in Android Studio SDK Manager, or start a connected Android device first.'
        }
        $availableAvds = @(& $emulatorPath -list-avds | Where-Object { $_.Trim() } | ForEach-Object { $_.Trim() })
        if ($LASTEXITCODE -ne 0) { throw 'Could not list AVDs. Open Android Studio Device Manager.' }
        if (-not $Avd) {
            if ($availableAvds.Count -eq 0) {
                throw 'No AVD is installed. Create one in Android Studio Device Manager.'
            }
            if ($availableAvds.Count -gt 1 -and $NoWindow) {
                throw "Create an AVD in Android Studio Device Manager, or choose one with -Avd NAME. Available AVDs: $($availableAvds -join ', ')"
            }
            $Avd = $availableAvds[0]
            Write-Host "Auto-selected Android AVD: $Avd (override with -Avd NAME)"
        }
        if ($Avd -notin $availableAvds) {
            throw "AVD '$Avd' does not exist. Available AVDs: $($availableAvds -join ', ')"
        }
        if ($Avd -notmatch '^[A-Za-z0-9_.-]+$') {
            throw 'Rename the AVD using only letters, numbers, underscores, dots or hyphens in Device Manager.'
        }
        # Preserve login/user data. Never pass -wipe-data when restarting this AVD.
        $emulatorArguments = @(
            '-avd', $Avd,
            '-feature', '-Vulkan',
            '-no-snapshot',
            '-no-audio'
        )
        if ($NoWindow) {
            $emulatorArguments += '-no-window'
        }
        $launchRoot = Join-Path (Split-Path (Split-Path $PSScriptRoot)) 'runtime\diagnostics\emulator-launches'
        [IO.Directory]::CreateDirectory($launchRoot) | Out-Null
        $launchId = (Get-Date).ToString('yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N')
        $stdoutPath = Join-Path $launchRoot "$launchId.stdout.log"
        $stderrPath = Join-Path $launchRoot "$launchId.stderr.log"
        $emulatorProcess = Start-Process -FilePath $emulatorPath -WindowStyle Hidden -ArgumentList $emulatorArguments -PassThru `
            -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
        @{ started_at=(Get-Date).ToString('o'); pid=$emulatorProcess.Id; executable=$emulatorPath;
            arguments=$emulatorArguments; stdout=$stdoutPath; stderr=$stderrPath } |
            ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $launchRoot "$launchId.json") -Encoding UTF8
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
$avdName = $null
if ($device -match '^emulator-\d+$') {
    $avdName = (& $adbPath -s $device emu avd name 2>$null | Where-Object { $_ -and $_ -ne 'OK' } | Select-Object -First 1)
}
if ($avdName) {
    Write-Host "Android ready: $device (AVD: $avdName)"
}
else {
    Write-Host "Android ready: $device"
}
