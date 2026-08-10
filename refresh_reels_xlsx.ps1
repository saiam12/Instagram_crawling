[CmdletBinding()]
param(
    [ValidateRange(1, 3600)]
    [double]$IntervalSeconds = 2,
    [ValidateRange(1, 3600)]
    [double]$FollowerIntervalSeconds = 8,
    [ValidateRange(0, 8760)]
    [double]$FollowerCacheHours = 1,
    [switch]$Manual,
    [switch]$Background,
    [string]$DataDir = ""
)

$ErrorActionPreference = "Stop"
if ($Manual -and $Background) {
    throw "-Manual and -Background cannot be used together."
}
$projectRoot = $PSScriptRoot
if (-not $DataDir) {
    $DataDir = Join-Path $projectRoot "data_web"
}

$workbookPath = Join-Path $DataDir "instagram_data.xlsx"
if (-not (Test-Path -LiteralPath $workbookPath)) {
    throw "instagram_data.xlsx was not found: $workbookPath"
}
try {
    $stream = [System.IO.File]::Open($workbookPath, 'Open', 'ReadWrite', 'None')
    $stream.Close()
} catch {
    throw "Close instagram_data.xlsx in Excel before running the refresh command."
}

$crawlerScript = Join-Path $projectRoot "instagram_reels_browser.mjs"
$collectorScript = Join-Path $projectRoot "instagram_collector.py"
$profileDir = Join-Path $projectRoot ".instagram_browser_profile"
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

$queuePath = Join-Path ([System.IO.Path]::GetTempPath()) ("instagram-reel-refresh-{0}.txt" -f [guid]::NewGuid())
$crawlerExitCode = 1
try {
    & $pythonExecutable $collectorScript "--data-dir" $DataDir "xlsx-reel-urls" "--output" $queuePath
    if ($LASTEXITCODE -ne 0) {
        throw "Could not read Reel URLs from instagram_data.xlsx."
    }

    $env:INSTAGRAM_PLAYWRIGHT_MODULE = $playwrightModule
    $env:INSTAGRAM_BROWSER_EXECUTABLE = $browserExecutable
    $arguments = @(
        $crawlerScript,
        "--urls-file", $queuePath,
        "--interval-seconds", $IntervalSeconds,
        "--follower-interval-seconds", $FollowerIntervalSeconds,
        "--follower-cache-hours", $FollowerCacheHours,
        "--data-dir", $DataDir,
        "--profile-dir", $profileDir
    )
    if ($Manual) {
        $arguments += "--manual"
    }
    if ($Background) {
        $arguments += "--background"
    }
    & $nodeExecutable @arguments
    $crawlerExitCode = $LASTEXITCODE
} finally {
    if (Test-Path -LiteralPath $queuePath) {
        Remove-Item -LiteralPath $queuePath -Force
    }
}

& $pythonExecutable $collectorScript "--data-dir" $DataDir "xlsx"
$xlsxExitCode = $LASTEXITCODE
if ($crawlerExitCode -ne 0) {
    exit $crawlerExitCode
}
exit $xlsxExitCode
