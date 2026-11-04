<##
.SYNOPSIS
Creates or repairs the virtual environment without changing collected data or the browser profile.
#>

[CmdletBinding()]
param(
    [string]$PythonExecutable
)

$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$venv = Join-Path $projectRoot ".venv"
$python = Join-Path $venv "Scripts\python.exe"
$requirements = Join-Path $projectRoot "requirements.txt"

$pythonArguments = @()
if (-not $PythonExecutable) {
    if (Get-Command py -CommandType Application -ErrorAction SilentlyContinue) {
        $PythonExecutable = 'py'
        $pythonArguments = @('-3')
    }
    elseif (Get-Command python -CommandType Application -ErrorAction SilentlyContinue) {
        $PythonExecutable = 'python'
    }
    else {
        throw 'Python not found. Install Python 3.12, or pass -PythonExecutable with the full path to python.exe.'
    }
}
if (-not (Get-Command $PythonExecutable -CommandType Application -ErrorAction SilentlyContinue)) {
    throw "Python executable not found: $PythonExecutable"
}
& $PythonExecutable @pythonArguments -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'
if ($LASTEXITCODE -ne 0) {
    throw 'Python 3.12 or newer is required. Install Python 3.12, or pass -PythonExecutable with a compatible python.exe path.'
}
& $PythonExecutable @pythonArguments -m venv --upgrade $venv
if ($LASTEXITCODE -ne 0) {
    throw 'Could not create or repair .venv. Close collectors using this environment and retry.'
}

$savedPythonPath = $env:PYTHONPATH
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
try {
    & $python -m pip install --ignore-installed -r $requirements
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
