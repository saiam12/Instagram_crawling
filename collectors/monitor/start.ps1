[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)][int]$Port = 8765,
    [string]$DataDir = 'data/collector',
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
if ($DataDir -match '["\r\n]') { throw 'DataDir must not contain quotes or newlines.' }
$collectors = Split-Path -Parent $PSScriptRoot
$python = Join-Path $collectors '.venv\Scripts\python.exe'
& (Join-Path $collectors 'scripts\setup\repair_venv.ps1') -IfNeeded
if ($LASTEXITCODE -ne 0) { throw 'Collector Python setup failed. Resolve the setup error above and retry.' }
$runtime = Join-Path $collectors 'runtime\monitor'
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
$url = "http://127.0.0.1:$Port"
$health = $null
try { $health = Invoke-RestMethod "$url/api/health" -TimeoutSec 2 } catch { }
if ($health -and $health.app -ne 'collector-monitor') { throw "Port $Port is already in use. Choose another -Port." }
if ($health) {
    $expected = [System.IO.Path]::GetFullPath((Join-Path $collectors $DataDir))
    if ($health.data_root -ne $expected) { throw "A monitor for another data folder is already running on port $Port. Use another -Port." }
}
if (-not $health) {
    $server = Join-Path $PSScriptRoot 'backend\server.py'
    $arguments = @('-u', '-B', ('"' + $server + '"'), '--port', "$Port", '--data-dir', ('"' + $DataDir + '"'))
    $process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $runtime 'server.out.log') -RedirectStandardError (Join-Path $runtime 'server.err.log')
    $deadline = (Get-Date).AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 300
        try { $health = Invoke-RestMethod "$url/api/health" -TimeoutSec 2 } catch { }
        if ($process.HasExited -and -not $health) { break }
    } while (-not $health -and (Get-Date) -lt $deadline)
    if (-not $health -or $health.app -ne 'collector-monitor') {
        throw "Monitor did not start. Check $runtime\server.err.log or choose another -Port."
    }
}
Write-Host "Collector Monitor: $url"
if (-not $NoBrowser) { Start-Process $url }
