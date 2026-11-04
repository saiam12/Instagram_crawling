param(
    [string]$DeviceId = $env:INSTAGRAM_ANDROID_DEVICE_ID,
    [string]$AdbPath = $env:INSTAGRAM_ANDROID_ADB_PATH,
    [string]$DataDir,
    [string]$OutputRoot,
    [switch]$Background,
    [switch]$Stop,
    [switch]$Status
)

# Read-only observation. Never restart Android, send input, or clear logcat.
$ErrorActionPreference = 'Stop'
$collectors = Split-Path (Split-Path $PSScriptRoot)
if (-not $OutputRoot) { $OutputRoot = Join-Path $collectors 'runtime\diagnostics\android-watch' }
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
[IO.Directory]::CreateDirectory($OutputRoot) | Out-Null
$controlPath = Join-Path $OutputRoot 'current.json'
if ($Stop -or $Status) {
    if (-not (Test-Path -LiteralPath $controlPath)) { Write-Host 'No diagnostic session found.'; return }
    $current = Get-Content -LiteralPath $controlPath -Raw | ConvertFrom-Json
    if ($Stop) {
        [IO.File]::WriteAllText((Join-Path $current.directory 'stop.request'), 'stop')
        Write-Host 'Stop requested for the diagnostic recorder only.'
    } else {
        $current
        $recorder = Get-Process -Id $current.pid -ErrorAction SilentlyContinue
        $alive = $false
        if ($recorder -and $current.process_started_at) {
            $alive = $recorder.StartTime.ToUniversalTime().Ticks -eq ([datetime]$current.process_started_at).ToUniversalTime().Ticks
        }
        Write-Host "Recorder alive: $alive (status below is the last saved observation)"
        Get-Content -LiteralPath (Join-Path $current.directory 'status.json') -Raw -ErrorAction SilentlyContinue
    }
    return
}
if (-not $AdbPath) {
    foreach ($sdk in @($env:ANDROID_HOME, $env:ANDROID_SDK_ROOT, "$env:LOCALAPPDATA\Android\Sdk")) {
        if ($sdk -and (Test-Path -LiteralPath (Join-Path $sdk 'platform-tools\adb.exe'))) {
            $AdbPath = Join-Path $sdk 'platform-tools\adb.exe'; break
        }
    }
    if (-not $AdbPath) {
        $command = Get-Command adb.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command) { $AdbPath = $command.Source }
    }
}
if (-not $AdbPath -or -not (Test-Path -LiteralPath $AdbPath -PathType Leaf)) { throw 'ADB not found. Pass -AdbPath or set ANDROID_HOME.' }
$AdbPath = (Resolve-Path -LiteralPath $AdbPath).Path
if (-not $DeviceId) {
    $savedErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $deviceOutput = @(& $AdbPath devices 2>&1)
        $deviceExitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $savedErrorActionPreference }
    if ($deviceExitCode -ne 0) { throw 'ADB device query failed. Pass -DeviceId after checking adb devices.' }
    $onlineDevices = @($deviceOutput | ForEach-Object { $_.ToString() } |
        Where-Object { $_ -match '^\S+\s+device$' } | ForEach-Object { ($_ -split '\s+')[0] })
    if ($onlineDevices.Count -ne 1) {
        throw 'Choose a diagnostic device with -DeviceId or INSTAGRAM_ANDROID_DEVICE_ID; exactly one online device is required for automatic selection.'
    }
    $DeviceId = $onlineDevices[0]
}
if ($DeviceId -notmatch '^[A-Za-z0-9_.:-]+$') { throw 'Invalid Android device serial.' }
if ($DataDir) { $DataDir = (Resolve-Path -LiteralPath $DataDir).Path }
if ($Background) {
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"",
        '-DeviceId', $DeviceId, '-AdbPath', "`"$AdbPath`"", '-OutputRoot', "`"$OutputRoot`"")
    if ($DataDir) { $arguments += @('-DataDir', "`"$DataDir`"") }
    $launcherId = [Guid]::NewGuid().ToString('N')
    $process = Start-Process -FilePath (Get-Process -Id $PID).Path -ArgumentList $arguments -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $OutputRoot "launcher-$launcherId.out.log") `
        -RedirectStandardError (Join-Path $OutputRoot "launcher-$launcherId.err.log")
    Write-Host "Recorder launched: PID $($process.Id). Use -Status to confirm recording: $OutputRoot"
    return
}

$hash = [Security.Cryptography.SHA256]::Create()
$lockId = [BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($OutputRoot.ToLowerInvariant()))).Replace('-', '')
$hash.Dispose()
$created = $false
$mutex = [Threading.Mutex]::new($true, "Local\InstagramAndroidDiagnostics-$lockId", [ref]$created)
if (-not $created) { $mutex.Dispose(); throw 'A diagnostic recorder is already running for this output directory.' }
$session = Join-Path $OutputRoot ((Get-Date).ToString('yyyyMMdd-HHmmss') + "-$PID")
[IO.Directory]::CreateDirectory($session) | Out-Null
$utf8 = [Text.UTF8Encoding]::new($false)
$script:logPart = 1
$script:eventPath = Join-Path $session 'events-001.jsonl'
function Write-Record($Kind, $Details) {
    if ((Test-Path -LiteralPath $script:eventPath) -and (Get-Item -LiteralPath $script:eventPath).Length -gt 20MB) {
        $script:logPart++
        $script:eventPath = Join-Path $session ('events-{0:000}.jsonl' -f $script:logPart)
    }
    $record = @{ at = (Get-Date).ToString('o'); kind = $Kind; details = $Details }
    [IO.File]::AppendAllText($script:eventPath, (($record | ConvertTo-Json -Depth 10 -Compress) + [Environment]::NewLine), $utf8)
}
function Write-JsonFile($Path, $Value) {
    [IO.File]::WriteAllText("$Path.tmp", ($Value | ConvertTo-Json -Depth 10), $utf8)
    Move-Item -LiteralPath "$Path.tmp" -Destination $Path -Force
}
function Invoke-AdbRead($Arguments, [int]$TimeoutMilliseconds = 4000) {
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $AdbPath
    $info.Arguments = "-s $DeviceId $Arguments"
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $child = [Diagnostics.Process]::new()
    $child.StartInfo = $info
    try {
        $child.Start() | Out-Null
        $stdout = $child.StandardOutput.ReadToEndAsync()
        $stderr = $child.StandardError.ReadToEndAsync()
        $exited = $child.WaitForExit($TimeoutMilliseconds)
        if (-not $exited) {
            # Only terminate this recorder's timed-out ADB client, never the ADB server.
            try { $child.Kill(); $child.WaitForExit(1000) | Out-Null } catch { }
        }
        $out = if ($stdout.IsCompleted) { $stdout.Result } else { '' }
        $err = if ($stderr.IsCompleted) { $stderr.Result } else { '' }
        return @{ timed_out = -not $exited; exit_code = $(if ($exited) { $child.ExitCode } else { $null });
            stdout = $out; stderr = $err }
    } finally { $child.Dispose() }
}
$seenWindowsEvents = [Collections.Generic.HashSet[string]]::new()
$script:windowsSince = (Get-Date).AddMinutes(-5)
function Save-WindowsEvents {
    $through = Get-Date
    foreach ($log in @('Application', 'System')) {
        $ids = if ($log -eq 'Application') { @(1000, 1001, 1002) } else { @(41, 42, 105, 2004, 4101, 6008, 7031, 7034) }
        $errors = @()
        $events = @(Get-WinEvent -FilterHashtable @{LogName=$log; Id=$ids; StartTime=$script:windowsSince; EndTime=$through} `
            -ErrorAction SilentlyContinue -ErrorVariable errors)
        foreach ($errorItem in $errors) {
            if ($errorItem.FullyQualifiedErrorId -notlike 'NoMatchingEventsFound*') {
                Write-Record 'windows_event_query_error' @{ log=$log; error=$errorItem.ToString() }
            }
        }
        foreach ($eventItem in $events) {
            if ($seenWindowsEvents.Add("$log/$($eventItem.RecordId)")) {
                Write-Record 'windows_event' @{ log=$log; id=$eventItem.Id; record_id=$eventItem.RecordId;
                    time=$eventItem.TimeCreated.ToString('o'); provider=$eventItem.ProviderName;
                    message=$eventItem.Message; xml=$eventItem.ToXml() }
            }
        }
    }
    # Overlap catches records published slightly after their occurrence time.
    $script:windowsSince = $through.AddMinutes(-5)
}
function Save-Incident($Reason) {
    $folder = Join-Path $session ('incident-' + (Get-Date).ToString('yyyyMMdd-HHmmss-fff'))
    [IO.Directory]::CreateDirectory($folder) | Out-Null
    Write-Record 'incident' @{ reason=$Reason; directory=$folder }
    if (Test-Path -LiteralPath (Join-Path $session 'android-buffer-latest.json')) {
        Copy-Item -LiteralPath (Join-Path $session 'android-buffer-latest.json') -Destination $folder
    }
    if (Test-Path -LiteralPath (Join-Path $session 'input-latest.json')) {
        Copy-Item -LiteralPath (Join-Path $session 'input-latest.json') -Destination $folder
    }
    if ($DataDir) {
        foreach ($name in @('android.log', 'python.log')) {
            $source = Join-Path $DataDir ".collector\$name"
            if (Test-Path -LiteralPath $source) {
                [IO.File]::WriteAllLines((Join-Path $folder $name), [string[]]@(Get-Content -LiteralPath $source -Tail 150), $utf8)
            }
        }
    }
}

$tracked = @{}
$denied = [Collections.Generic.HashSet[int]]::new()
$lastState = ''
$lastPeriodic = [datetime]::MinValue
$statusPath = Join-Path $session 'status.json'
try {
    Write-JsonFile $controlPath @{ pid=$PID; started_at=(Get-Date).ToString('o'); directory=$session; device_id=$DeviceId;
        process_started_at=(Get-Process -Id $PID).StartTime.ToString('o') }
    Write-Record 'recorder_started' @{ pid=$PID; device_id=$DeviceId; adb=$AdbPath; data_dir=$DataDir;
        interval_seconds=10; adb_timeout_seconds=4; note='Process/ADB observation; no restart, input, log clearing, or crash-report upload.' }
    Write-Host "Recording: $session"
    while (-not (Test-Path -LiteralPath (Join-Path $session 'stop.request'))) {
        $cycle = [Diagnostics.Stopwatch]::StartNew()
        try {
            foreach ($key in @($tracked.Keys)) {
                $item = $tracked[$key]
                if ($item.process.HasExited) {
                    $code = $null
                    $exitError = $null
                    try { $code = $item.process.ExitCode } catch { $exitError = $_.ToString() }
                    Write-Record 'process_exit' @{ pid=$key; name=$item.name; path=$item.path;
                        exit_code=$code; exit_code_hex=$(if ($null -ne $code) { '0x{0:X8}' -f $code } else { $null });
                        error=$exitError; note='Exit code alone cannot distinguish a user close from every crash.' }
                    $item.process.Dispose()
                    $tracked.Remove($key)
                    Save-Incident "process_exit:$key"
                }
            }
            $samples = @()
            $processes = @(Get-Process -Name 'emulator', 'qemu-system-*', 'crashpad_handler' -ErrorAction SilentlyContinue)
            foreach ($candidate in $processes) {
                $key = $candidate.Id
                if (-not $tracked.ContainsKey($key)) {
                    try {
                        $path = $candidate.Path
                        # Exclude crash reporters belonging to browsers and unrelated applications.
                        if ($candidate.ProcessName -eq 'crashpad_handler' -and $path -notlike '*\emulator\*') { $candidate.Dispose(); continue }
                        $handle = $candidate.Handle # Keep a real handle open until exit, including non-child processes.
                        $startedAt = $candidate.StartTime.ToString('o')
                        $tracked[$key] = @{ process=$candidate; name=$candidate.ProcessName; path=$path }
                        Write-Record 'process_attached' @{ pid=$key; name=$candidate.ProcessName; path=$path;
                            started_at=$startedAt; handle_opened=$true }
                    } catch {
                        if ($denied.Add($key)) { Write-Record 'process_attach_error' @{ pid=$key; error=$_.ToString() } }
                        $candidate.Dispose(); continue
                    }
                } else { $candidate.Dispose() }
                $watched = $tracked[$key].process
                try {
                    $watched.Refresh()
                    $samples += @{ pid=$key; name=$tracked[$key].name; private_bytes=$watched.PrivateMemorySize64;
                        working_set_bytes=$watched.WorkingSet64; handles=$watched.HandleCount; cpu_seconds=$watched.TotalProcessorTime.TotalSeconds }
                } catch { Write-Record 'process_sample_error' @{ pid=$key; error=$_.ToString() } }
            }
            $probe = Invoke-AdbRead 'shell getprop sys.boot_completed'
            $state = if ($probe.timed_out) { 'adb_timeout' } elseif ($probe.exit_code -ne 0) { 'adb_error' } elseif ($probe.stdout.Trim() -eq '1') { 'boot_completed' } else { 'booting' }
            if ($state -ne $lastState) {
                Write-Record 'adb_state_changed' @{ previous=$lastState; current=$state; probe=$probe }
                if ($state -in @('adb_timeout', 'adb_error')) { Save-Incident $state }
                $lastState = $state
            }
            $health = @{ at=(Get-Date).ToString('o'); recorder_pid=$PID; device_id=$DeviceId; state=$state;
                adb=$probe; processes=$samples; directory=$session }
            Write-Record 'sample' $health
            Write-JsonFile $statusPath $health
            if (((Get-Date) - $lastPeriodic).TotalSeconds -ge 60) {
                Save-WindowsEvents
                $os = Get-CimInstance Win32_OperatingSystem
                Write-Record 'host_memory' @{ free_physical_kb=$os.FreePhysicalMemory; total_physical_kb=$os.TotalVisibleMemorySize;
                    free_virtual_kb=$os.FreeVirtualMemory; total_virtual_kb=$os.TotalVirtualMemorySize; last_boot=$os.LastBootUpTime.ToString('o') }
                if ($state -eq 'boot_completed') {
                    $inputProbe = Invoke-AdbRead 'shell dumpsys input'
                    $inputBlocked = $inputProbe.stdout -match '\bDispatchEnabled:\s*(?:false|0)\b|\bDispatchFrozen:\s*(?:true|1)\b'
                    Write-Record 'input_dispatch' @{ timed_out=$inputProbe.timed_out; exit_code=$inputProbe.exit_code;
                        stderr=$inputProbe.stderr; dispatch_blocked=$inputBlocked }
                    if (-not $inputProbe.timed_out -and $inputProbe.exit_code -eq 0) {
                        Write-JsonFile (Join-Path $session 'input-latest.json') @{ captured_at=(Get-Date).ToString('o'); result=$inputProbe }
                    }
                    if ($inputBlocked) { Save-Incident 'input_dispatch_blocked' }
                    $buffer = Invoke-AdbRead 'logcat -d -b crash -b system -v threadtime -t 300'
                    if (-not $buffer.timed_out -and $buffer.exit_code -eq 0) {
                        Write-JsonFile (Join-Path $session 'android-buffer-latest.json') @{ captured_at=(Get-Date).ToString('o'); result=$buffer }
                    } else { Write-Record 'android_buffer_error' $buffer }
                }
                $lastPeriodic = Get-Date
            }
        } catch { Write-Record 'recorder_error' @{ error=$_.ToString(); stack=$_.ScriptStackTrace } }
        # A stop request is checked each second, even between sampling intervals.
        while ($cycle.Elapsed.TotalSeconds -lt 10 -and -not (Test-Path -LiteralPath (Join-Path $session 'stop.request'))) { Start-Sleep -Seconds 1 }
    }
} finally {
    foreach ($item in $tracked.Values) { $item.process.Dispose() }
    try {
        Save-WindowsEvents
        Write-Record 'recorder_stopped' @{ pid=$PID }
        Write-JsonFile $statusPath @{ at=(Get-Date).ToString('o'); state='recorder_stopped'; recorder_pid=$PID; directory=$session }
    } finally { $mutex.ReleaseMutex(); $mutex.Dispose() }
}
