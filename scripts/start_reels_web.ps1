[CmdletBinding()]
param(
    [int]$MaxItems = 50,
    [double]$IntervalSeconds = 5,
    [ValidateRange(1, 3600)]
    [double]$FollowerIntervalSeconds = 8,
    [ValidateRange(0, 8760)]
    [double]$FollowerCacheHours = 1,
    [switch]$Manual,
    [switch]$Background,
    [string]$HashtagQuery = "",
    [string]$StartUrl = "https://www.instagram.com/reels/",
    [string]$DataDir = ""
)

$ErrorActionPreference = "Stop"
if ($Manual -and $Background) {
    throw "-Manual and -Background cannot be used together."
}
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $DataDir) {
    $DataDir = Join-Path $projectRoot "data_web"
}
$profileDir = Join-Path $projectRoot ".instagram_browser_profile"
$crawlerScript = Join-Path $projectRoot "collectors\instagram_reels_browser.mjs"
$collectorScript = Join-Path $projectRoot "exporters\instagram_collector.py"
$bundledRoot = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies"
$bundledNode = Join-Path $bundledRoot "node\bin\node.exe"
$bundledPlaywright = Join-Path $bundledRoot "node\node_modules\playwright"
$bundledPython = Join-Path $bundledRoot "python\python.exe"

$nodeExecutable = if (Test-Path -LiteralPath $bundledNode) {
    $bundledNode
} else {
    (Get-Command node -ErrorAction Stop).Source
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

$env:INSTAGRAM_PLAYWRIGHT_MODULE = $playwrightModule
$env:INSTAGRAM_BROWSER_EXECUTABLE = $browserExecutable
$arguments = @(
    $crawlerScript,
    "--start-url", $StartUrl,
    "--max-items", $MaxItems,
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
if ($HashtagQuery) {
    $arguments += @("--hashtag-query", $HashtagQuery)
}

$crawlerExitCode = 1
$xlsxExitCode = 0
try {
    & $nodeExecutable @arguments
    $crawlerExitCode = $LASTEXITCODE
} finally {
    $pythonExecutable = if (Test-Path -LiteralPath $bundledPython) {
        $bundledPython
    } else {
        (Get-Command python -ErrorAction Stop).Source
    }
    $workbookPath = Join-Path $DataDir "instagram_data.xlsx"
    $workbookLocked = $false
    if (Test-Path -LiteralPath $workbookPath) {
        try {
            $stream = [System.IO.File]::Open($workbookPath, 'Open', 'ReadWrite', 'None')
            $stream.Close()
        } catch [System.IO.IOException] {
            $workbookLocked = $true
        } catch [System.UnauthorizedAccessException] {
            $workbookLocked = $true
        }
    }
    if ($workbookLocked) {
        $updatedWorkbookPath = Join-Path $DataDir "instagram_data_updated.xlsx"
        & $pythonExecutable $collectorScript "--data-dir" $DataDir "xlsx" "--output" $updatedWorkbookPath
        $xlsxExitCode = $LASTEXITCODE
        if ($xlsxExitCode -eq 0) {
            Write-Warning "instagram_data.xlsx is open in Excel. The latest Reel and follower results were saved to instagram_data_updated.xlsx instead."
        }
    } else {
        & $pythonExecutable $collectorScript "--data-dir" $DataDir "xlsx"
        $xlsxExitCode = $LASTEXITCODE
    }
}

if ($crawlerExitCode -eq 0 -and $xlsxExitCode -ne 0) {
    exit $xlsxExitCode
}
exit $crawlerExitCode
