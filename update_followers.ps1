[CmdletBinding()]
param(
    [string]$DataDir = "",
    [ValidateRange(1, 3600)]
    [double]$IntervalSeconds = 8,
    [ValidateRange(0, 8760)]
    [double]$CacheHours = 1,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
if (-not $DataDir) {
    $DataDir = Join-Path $projectRoot "data_web"
}

$crawlerScript = Join-Path $projectRoot "instagram_reels_browser.mjs"
$collectorScript = Join-Path $projectRoot "instagram_collector.py"
$bundledRoot = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies"
$bundledNode = Join-Path $bundledRoot "node\bin\node.exe"
$bundledPlaywright = Join-Path $bundledRoot "node\node_modules\playwright"
$bundledPython = Join-Path $bundledRoot "python\python.exe"

$nodeExecutable = if (Test-Path -LiteralPath $bundledNode) {
    $bundledNode
} else {
    (Get-Command node -ErrorAction Stop).Source
}
$pythonExecutable = if (Test-Path -LiteralPath $bundledPython) {
    $bundledPython
} else {
    (Get-Command python -ErrorAction Stop).Source
}
$playwrightModule = if (Test-Path -LiteralPath $bundledPlaywright) {
    $bundledPlaywright
} else {
    Join-Path $projectRoot "node_modules\playwright"
}
if (-not (Test-Path -LiteralPath $playwrightModule)) {
    throw "Playwright was not found. Install it in the project with: npm install playwright"
}

$edgePath = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
$chromePath = "C:\Program Files\Google\Chrome\Application\chrome.exe"
$browserExecutable = if (Test-Path -LiteralPath $edgePath) {
    $edgePath
} elseif (Test-Path -LiteralPath $chromePath) {
    $chromePath
} else {
    throw "Microsoft Edge or Google Chrome was not found."
}
$profileDir = Join-Path $projectRoot ".instagram_browser_profile"
$env:INSTAGRAM_PLAYWRIGHT_MODULE = $playwrightModule
$env:INSTAGRAM_BROWSER_EXECUTABLE = $browserExecutable

function Test-WorkbookLocked {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return $false
    }
    try {
        $stream = [System.IO.File]::Open($Path, 'Open', 'ReadWrite', 'None')
        $stream.Close()
        return $false
    } catch [System.IO.IOException] {
        return $true
    } catch [System.UnauthorizedAccessException] {
        return $true
    }
}

$arguments = @(
    $crawlerScript,
    "--followers-only",
    "--background",
    "--data-dir", $DataDir,
    "--profile-dir", $profileDir,
    "--follower-interval-seconds", $IntervalSeconds,
    "--follower-cache-hours", $CacheHours
)
if ($Force) {
    $arguments += "--force-followers"
}

& $nodeExecutable @arguments
$followerExitCode = $LASTEXITCODE
$workbookPath = Join-Path $DataDir "instagram_data.xlsx"
$xlsxExitCode = 0
if (Test-WorkbookLocked -Path $workbookPath) {
    $updatedWorkbookPath = Join-Path $DataDir "instagram_data_updated.xlsx"
    & $pythonExecutable $collectorScript "--data-dir" $DataDir "xlsx" "--output" $updatedWorkbookPath
    $xlsxExitCode = $LASTEXITCODE
    if ($xlsxExitCode -eq 0) {
        Write-Warning "instagram_data.xlsx is open in Excel. The latest follower results were saved to instagram_data_updated.xlsx instead."
    }
} else {
    & $pythonExecutable $collectorScript "--data-dir" $DataDir "xlsx"
    $xlsxExitCode = $LASTEXITCODE
}
if ($followerExitCode -eq 0 -and $xlsxExitCode -ne 0) {
    exit $xlsxExitCode
}
exit $followerExitCode
