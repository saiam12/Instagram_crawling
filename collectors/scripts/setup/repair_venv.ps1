<##
.SYNOPSIS
Creates or repairs the virtual environment without changing collected data or the browser profile.
#>

[CmdletBinding()]
param(
    [string]$PythonExecutable,
    [switch]$IfNeeded
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$venv = Join-Path $projectRoot ".venv"
$python = Join-Path $venv "Scripts\python.exe"
$requirements = Join-Path $projectRoot "requirements.txt"
$stamp = Join-Path $venv '.requirements.sha256'
$hasher = [System.Security.Cryptography.SHA256]::Create()
try { $requirementsHash = [BitConverter]::ToString($hasher.ComputeHash([IO.File]::ReadAllBytes($requirements))) }
finally { $hasher.Dispose() }
$probe = 'import sys, playwright.sync_api, playwright.async_api; sys.exit(0 if sys.version_info >= (3, 12) and sys.prefix != sys.base_prefix else 1)'
$healthy = $false
if ($IfNeeded -and (Test-Path -LiteralPath $python -PathType Leaf)) {
    try {
        & $python -I -c $probe 2>$null
        $healthy = $LASTEXITCODE -eq 0
    } catch { $healthy = $false }
    if ($healthy -and (Test-Path -LiteralPath $stamp) -and
        (Get-Content -LiteralPath $stamp -Raw) -eq $requirementsHash) {
        exit 0
    }
}

Write-Host 'Preparing collector Python environment (first run, changed requirements, or repair)...'

$pythonArguments = @()
if (-not $healthy) {
    if (-not $PythonExecutable) {
        foreach ($candidate in @(
            @{ Name = 'py'; Arguments = @('-3.12') },
            @{ Name = 'py'; Arguments = @('-3') },
            @{ Name = 'python'; Arguments = @() }
        )) {
            $command = Get-Command $candidate.Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if (-not $command) { continue }
            $candidateArguments = $candidate.Arguments
            try {
                & $command.Source @candidateArguments -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>$null
                if ($LASTEXITCODE -ne 0) { continue }
            } catch { continue }
            $PythonExecutable = $command.Source
            $pythonArguments = $candidateArguments
            break
        }
        if (-not $PythonExecutable) {
            throw 'Python 3.12+ not found. Install Python 3.12, or pass -PythonExecutable with the full path to python.exe.'
        }
    }
    if (-not (Get-Command $PythonExecutable -CommandType Application -ErrorAction SilentlyContinue)) {
        throw "Python executable not found: $PythonExecutable"
    }
    & $PythonExecutable @pythonArguments -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'
    if ($LASTEXITCODE -ne 0) {
        throw 'Python 3.12 or newer is required. Install Python 3.12, or pass -PythonExecutable with a compatible python.exe path.'
    }
    & $PythonExecutable @pythonArguments -I -m venv --upgrade $venv
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not create or repair .venv. Close collectors using this environment and retry.'
    }
}

$savedPythonPath = $env:PYTHONPATH
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
try {
    $installArguments = @('-I', '-m', 'pip', 'install', '-r', $requirements)
    if (-not $healthy) { $installArguments += '--ignore-installed' }
    & $python @installArguments
    if ($LASTEXITCODE -ne 0) { throw 'Collector package installation failed. Check the pip error above and retry.' }
    & $python -I -c $probe
    if ($LASTEXITCODE -ne 0) { throw 'Collector environment validation failed. Run scripts/setup/repair_venv.ps1 to repair it.' }
    Set-Content -LiteralPath $stamp -Value $requirementsHash -Encoding ASCII -NoNewline
    exit 0
}
finally {
    if ($null -eq $savedPythonPath) {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONPATH = $savedPythonPath
    }
}
