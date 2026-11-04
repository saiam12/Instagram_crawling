<##
.SYNOPSIS
Runs the Python Instagram collector with this project's virtual environment.

.EXAMPLE
.\collector.ps1 --max-items 50 --followers-after-reels
.\collector.ps1 refresh --background
.\collector.ps1 reconcile
.\collector.ps1 hashtag-posts --hashtag-query '오오티디 OR 패션'
.\collector.ps1 hashtag-posts --preset fashion-beauty
.\collector.ps1 fashion
.\collector.ps1 fashion --collector-mode web
.\collector.ps1 fashion --collector-mode hybrid
.\collector.ps1 -fashion --background
#>

[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$Avd = $env:INSTAGRAM_ANDROID_AVD,
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$CollectorArguments
)

$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$launcher = Join-Path $projectRoot "scripts\instagram_reels_python.py"

if (-not (Test-Path -LiteralPath $python)) {
    Write-Error "Python virtual environment was not found. Run .\scripts\setup\repair_venv.ps1 from $projectRoot first."
    exit 1
}

$savedPythonPath = $env:PYTHONPATH
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
try {
    $argumentsToPass = @($CollectorArguments)
    # Keep the compact commands used in earlier runs.  They are commands,
    # not collector flags, so translate them before the normal "collect"
    # fallback handles dash-prefixed collector options.
    $commandShortcuts = @{
        "-fashion" = "fashion"
        "-beauty" = "beauty"
        "-fashion-beauty" = "fashion-beauty"
    }
    if ($argumentsToPass.Count -gt 0 -and $commandShortcuts.ContainsKey($argumentsToPass[0].ToLowerInvariant())) {
        $argumentsToPass[0] = $commandShortcuts[$argumentsToPass[0].ToLowerInvariant()]
    }
    elseif ($argumentsToPass.Count -eq 0 -or $argumentsToPass[0].StartsWith("-")) {
        $argumentsToPass = @("collect") + $argumentsToPass
    }
    foreach ($argument in $argumentsToPass) {
        if (($argument -split '=', 2)[0] -in @('--no-login', '--followers-only', '--followres-only')) {
            throw "Removed option: $argument. Use the followers command for follower updates."
        }
    }
    $androidCommands = @('collect', 'refresh', 'android-worker', 'hashtag-posts', 'fashion', 'beauty', 'fashion-beauty')
    $collectorMode = 'hybrid'
    $explicitCollectorMode = $false
    for ($index = 0; $index -lt $argumentsToPass.Count; $index++) {
        $argument = [string]$argumentsToPass[$index]
        $requestedMode = $null
        if ($argument -eq '--android-only') {
            throw '--android-only is no longer supported. Choose --collector-mode web or hybrid.'
        }
        elseif ($argument -eq '--collector-mode') {
            if (($index + 1) -ge $argumentsToPass.Count) {
                throw '--collector-mode requires one of: hybrid, web'
            }
            $requestedMode = ([string]$argumentsToPass[$index + 1]).Trim().ToLowerInvariant()
            $index++
        }
        elseif ($argument.StartsWith('--collector-mode=')) {
            $requestedMode = $argument.Split('=', 2)[1].Trim().ToLowerInvariant()
        }
        elseif ($argument -eq '--no-android-metrics') {
            $requestedMode = 'web'
        }
        elseif ($argument -eq '--android-metrics') {
            $requestedMode = 'hybrid'
        }
        if ($null -ne $requestedMode) {
            if (@('hybrid', 'web') -notcontains $requestedMode) {
                throw "Unknown collector mode '$requestedMode'. Choose hybrid or web."
            }
            if ($explicitCollectorMode -and $collectorMode -ne $requestedMode) {
                throw "Conflicting collector modes: $collectorMode and $requestedMode"
            }
            $collectorMode = $requestedMode
            $explicitCollectorMode = $true
        }
    }
    $helpRequested = ($argumentsToPass -contains '--help') -or ($argumentsToPass -contains '-h')
    if ($androidCommands -contains $argumentsToPass[0].ToLowerInvariant() -and $collectorMode -eq 'hybrid' -and -not $helpRequested) {
        $androidNoWindow = $argumentsToPass -contains '--background'
        $androidOptions = @{ Avd = $Avd; NoWindow = $androidNoWindow }
        for ($index = 0; $index -lt $argumentsToPass.Count; $index++) {
            $parts = ([string]$argumentsToPass[$index]) -split '=', 2
            $option = switch ($parts[0]) {
                '--android-adb-path' { 'AdbPath' }
                '--android-device-id' { 'DeviceId' }
            }
            if ($option) {
                if ($parts.Count -eq 2) { $value = $parts[1] }
                elseif (($index + 1) -lt $argumentsToPass.Count) { $value = $argumentsToPass[++$index] }
                else { throw "$($parts[0]) requires a value." }
                $androidOptions[$option] = $value
            }
        }
        & (Join-Path $projectRoot 'scripts\android\start-android.ps1') @androidOptions
        & $python -m reels.android_uiautomator
        if ($LASTEXITCODE -ne 0) { throw 'Android UI preparation failed. Follow the setup message above.' }
    }
    & $python $launcher @argumentsToPass
    exit $LASTEXITCODE
}
finally {
    if ($null -eq $savedPythonPath) {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONPATH = $savedPythonPath
    }
}
