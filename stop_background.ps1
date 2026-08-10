[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$dataRoot = Join-Path $PSScriptRoot "data"
$pidFile = Join-Path $dataRoot "background_collector.pid"

if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Output "Background collector is not registered as running."
    exit 0
}

$collectorPid = [int](Get-Content -LiteralPath $pidFile -Raw)
$collectorProcess = Get-Process -Id $collectorPid -ErrorAction SilentlyContinue
if ($collectorProcess) {
    Stop-Process -Id $collectorPid
    Write-Output "Background collector stopped. PID=$collectorPid"
} else {
    Write-Output "The saved process is no longer running."
}
Remove-Item -LiteralPath $pidFile -Force
