[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$record = Join-Path (Split-Path -Parent $PSScriptRoot) 'runtime\monitor\server.json'
if (-not (Test-Path -LiteralPath $record)) { Write-Host 'Monitor is not running.'; return }
$state = Get-Content -LiteralPath $record -Raw | ConvertFrom-Json
$serverPath = Join-Path $PSScriptRoot 'backend\server.py'
$process = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$state.pid)"
if (-not $process) { Write-Host 'Monitor is already stopped.'; return }
if ($process.Name -ne 'python.exe' -or -not $process.CommandLine -or $process.CommandLine.IndexOf($serverPath, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) {
    throw 'Stored PID does not belong to this monitor. No process was stopped.'
}
Stop-Process -Id $process.ProcessId
Write-Host 'Monitor stopped. Collectors and Android workers were not stopped.'
