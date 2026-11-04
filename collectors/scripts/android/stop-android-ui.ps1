param([int]$Port = 4723)
$ErrorActionPreference = 'Stop'
$statePath = Join-Path (Split-Path (Split-Path $PSScriptRoot)) "runtime\android-ui\server-$Port.json"
if (-not (Test-Path -LiteralPath $statePath)) { Write-Host 'No managed Android UI server was found.'; return }
$state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
$process = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$state.pid)"
if ($process) {
    if ($process.Name -ne 'node.exe' -or $process.CommandLine.IndexOf([string]$state.script, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw 'The saved PID belongs to another process. The server was not stopped.'
    }
    $sessions = Invoke-RestMethod -Uri "$($state.server_url)/appium/sessions" -TimeoutSec 5
    if (@($sessions.value).Count -gt 0) { throw 'Android UI sessions are still active. Stop collection before stopping this server.' }
    Stop-Process -Id ([int]$state.pid) -ErrorAction Stop
}
Remove-Item -LiteralPath $statePath
Write-Host 'Android UI server stopped. The emulator remains running.'
