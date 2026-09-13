<##
.SYNOPSIS
Repairs the Python 3.12 virtual environment without changing collected data or the browser profile.
#>

[CmdletBinding()]
param()

$projectRoot = $PSScriptRoot
$venv = Join-Path $projectRoot ".venv"
$python = Join-Path $venv "Scripts\python.exe"
$requirements = Join-Path $projectRoot "requirements.txt"

& py -3.12 -m venv --upgrade $venv
if ($LASTEXITCODE -ne 0) {
    Write-Error "Python 3.12 could not repair .venv. Install Python 3.12 with the Python launcher, then run this script again."
    exit $LASTEXITCODE
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
