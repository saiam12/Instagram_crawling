[CmdletBinding()]
param(
    [string]$ExperimentId = "",
    [double]$PollMinutes = 1,
    [double]$UsageThreshold = 90,
    [int]$MaxPosts = 0
)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$dataRoot = Join-Path $projectRoot "data"
$pidFile = Join-Path $dataRoot "background_collector.pid"
$stdoutLog = Join-Path $dataRoot "background_collector.log"
$stderrLog = Join-Path $dataRoot "background_collector.error.log"
$collectorScript = Join-Path $projectRoot "instagram_collector.py"

New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null

if (Test-Path -LiteralPath $pidFile) {
    $existingPid = [int](Get-Content -LiteralPath $pidFile -Raw)
    if (Get-Process -Id $existingPid -ErrorAction SilentlyContinue) {
        Write-Output "Background collector is already running. PID=$existingPid"
        exit 0
    }
    Remove-Item -LiteralPath $pidFile -Force
}

$pythonLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($pythonLauncher) {
    $pythonExecutable = $pythonLauncher.Source
    $collectorArguments = @("-3", "-u", $collectorScript)
} else {
    $pythonLauncher = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonLauncher) {
        throw "Python 3 was not found. Install Python or make py/python available in PATH."
    }
    $pythonExecutable = $pythonLauncher.Source
    $collectorArguments = @("-u", $collectorScript)
}

$collectorArguments += @(
    "--data-dir", $dataRoot,
    "experiment", "watch",
    "--poll-minutes", $PollMinutes,
    "--usage-threshold", $UsageThreshold,
    "--max-posts", $MaxPosts
)
if ($ExperimentId) {
    $collectorArguments += @("--experiment-id", $ExperimentId)
}

$collectorProcess = Start-Process `
    -FilePath $pythonExecutable `
    -ArgumentList $collectorArguments `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -PassThru

Set-Content -LiteralPath $pidFile -Value $collectorProcess.Id -Encoding Ascii
Write-Output "Background collector started. PID=$($collectorProcess.Id)"
Write-Output "Log: $stdoutLog"
Write-Output "Error log: $stderrLog"
