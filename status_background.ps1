[CmdletBinding()]
param()

$dataRoot = Join-Path $PSScriptRoot "data"
$pidFile = Join-Path $dataRoot "background_collector.pid"
$stdoutLog = Join-Path $dataRoot "background_collector.log"
$stderrLog = Join-Path $dataRoot "background_collector.error.log"

if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Output "Background collector: stopped"
    exit 0
}

$collectorPid = [int](Get-Content -LiteralPath $pidFile -Raw)
$collectorProcess = Get-Process -Id $collectorPid -ErrorAction SilentlyContinue
if ($collectorProcess) {
    Write-Output "Background collector: running (PID=$collectorPid)"
} else {
    Write-Output "Background collector: stale PID file (PID=$collectorPid)"
}
Write-Output "Log: $stdoutLog"
Write-Output "Error log: $stderrLog"
