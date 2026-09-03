<##
.SYNOPSIS
Runs the Android emulator Instagram collector using its dedicated Python environment.

.EXAMPLE
.\collector.ps1 feed --max-items 10
.\collector.ps1 hashtag --hashtag '패션 OR ootd' --max-items 10
.\collector.ps1 collect --fashion --max-items 50
#>

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CollectorArguments
)

$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$launcher = Join-Path $projectRoot "collect_android_reels.py"

if (-not (Test-Path -LiteralPath $python)) {
    Write-Error "Python virtual environment was not found. Run 'python -m venv .venv' and '.\.venv\Scripts\python.exe -m pip install -r requirements.txt' from $projectRoot first."
    exit 1
}

$savedPythonPath = $env:PYTHONPATH
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
try {
    & $python -X utf8 $launcher @CollectorArguments
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
