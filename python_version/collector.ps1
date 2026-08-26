<##
.SYNOPSIS
Runs the Python Instagram collector with this project's virtual environment.

.EXAMPLE
.\collector.ps1 --max-items 50 --followers-after-reels
.\collector.ps1 refresh --background
.\collector.ps1 reconcile
#>

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CollectorArguments
)

$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$launcher = Join-Path $projectRoot "scripts\instagram_reels_python.py"

if (-not (Test-Path -LiteralPath $python)) {
    Write-Error "Python virtual environment was not found. Run .\repair_venv.ps1 from $projectRoot first."
    exit 1
}

$savedPythonPath = $env:PYTHONPATH
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
try {
    $argumentsToPass = @($CollectorArguments)
    if ($argumentsToPass.Count -eq 0 -or $argumentsToPass[0].StartsWith("-")) {
        $argumentsToPass = @("collect") + $argumentsToPass
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
