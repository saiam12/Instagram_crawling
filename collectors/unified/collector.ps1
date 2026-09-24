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
.\collector.ps1 fashion --collector-mode android
.\collector.ps1 -fashion --background
#>

[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$Avd = 'Pixel_8_clean',
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$CollectorArguments
)

$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$launcher = Join-Path $projectRoot "scripts\instagram_reels_python.py"

if (-not (Test-Path -LiteralPath $python)) {
    Write-Error "Python virtual environment was not found. Run .\scripts\repair_venv.ps1 from $projectRoot first."
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
    $androidCommands = @('collect', 'refresh', 'android-worker', 'hashtag-posts', 'fashion', 'beauty', 'fashion-beauty')
    $collectorMode = 'hybrid'
    $explicitCollectorMode = $false
    for ($index = 0; $index -lt $argumentsToPass.Count; $index++) {
        $argument = [string]$argumentsToPass[$index]
        if ($argument -eq '--android-only') {
            $collectorMode = 'android'
            $explicitCollectorMode = $true
        }
        elseif ($argument -eq '--collector-mode' -and ($index + 1) -lt $argumentsToPass.Count) {
            $collectorMode = ([string]$argumentsToPass[$index + 1]).ToLowerInvariant()
            $explicitCollectorMode = $true
            $index++
        }
        elseif ($argument.StartsWith('--collector-mode=')) {
            $collectorMode = $argument.Split('=', 2)[1].ToLowerInvariant()
            $explicitCollectorMode = $true
        }
        elseif ($argument -eq '--no-android-metrics' -and -not $explicitCollectorMode) {
            $collectorMode = 'web'
        }
    }
    if (@('hybrid', 'web', 'android') -notcontains $collectorMode) {
        throw "Unknown collector mode '$collectorMode'. Choose hybrid, web, or android."
    }
    if ($androidCommands -contains $argumentsToPass[0].ToLowerInvariant() -and $collectorMode -ne 'web' -and -not ($argumentsToPass -contains '--no-android-metrics')) {
        $androidNoWindow = $argumentsToPass -contains '--background'
        & (Join-Path $projectRoot 'scripts\start-android.ps1') -Avd $Avd -NoWindow:$androidNoWindow
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
