<#
.SYNOPSIS
Rebuilds instagram_data.xlsx from the CSV files and records their synchronized state.

.EXAMPLE
.\scripts\sync_xlsx.ps1

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\sync_xlsx.ps1 -Force

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\sync_xlsx.ps1 -WaitForUnlockMinutes 480
#>
[CmdletBinding()]
param(
    [string]$DataDir = "",
    [string]$Output = "",
    [switch]$Force,
    [ValidateRange(0, 10080)]
    [int]$WaitForUnlockMinutes = 0,
    [ValidateRange(1, 3600)]
    [int]$UnlockPollSeconds = 15
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $DataDir) {
    $DataDir = Join-Path $projectRoot "data_web"
}
$DataDir = [System.IO.Path]::GetFullPath($DataDir)
if (-not (Test-Path -LiteralPath $DataDir -PathType Container)) {
    throw "Data directory was not found: $DataDir"
}

$collectorScript = Join-Path $projectRoot "exporters\instagram_collector.py"
if (-not (Test-Path -LiteralPath $collectorScript -PathType Leaf)) {
    throw "XLSX exporter was not found: $collectorScript"
}

$bundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$pythonExecutable = if (Test-Path -LiteralPath $bundledPython -PathType Leaf) {
    $bundledPython
} else {
    (Get-Command python -ErrorAction Stop).Source
}

function Get-CsvSnapshot {
    param([string]$Directory)

    $csvFiles = @(Get-ChildItem -LiteralPath $Directory -File -Filter "*.csv" | Sort-Object Name)
    if ($csvFiles.Count -eq 0) {
        throw "No CSV files were found in: $Directory"
    }
    return @($csvFiles | ForEach-Object {
        [ordered]@{
            name = $_.Name
            length = $_.Length
            last_write_utc = $_.LastWriteTimeUtc.ToString("O")
            sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    })
}

function Get-SnapshotFingerprint {
    param([object[]]$Snapshot)

    $json = ConvertTo-Json -InputObject $Snapshot -Depth 5 -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        return [System.BitConverter]::ToString($sha256.ComputeHash($bytes)).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha256.Dispose()
    }
}

function Test-FileLocked {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }
    try {
        $stream = [System.IO.File]::Open($Path, "Open", "ReadWrite", "None")
        $stream.Close()
        return $false
    } catch [System.IO.IOException] {
        return $true
    } catch [System.UnauthorizedAccessException] {
        return $true
    }
}

function Test-XlsxPackage {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    try {
        $archive = [System.IO.Compression.ZipFile]::OpenRead($Path)
        try {
            $entryNames = @($archive.Entries | ForEach-Object FullName)
            return $entryNames -contains "xl/workbook.xml" -and
                $entryNames -contains "[Content_Types].xml" -and
                @($entryNames | Where-Object { $_ -like "xl/worksheets/sheet*.xml" }).Count -gt 0
        } finally {
            $archive.Dispose()
        }
    } catch {
        return $false
    }
}

$requestedWorkbook = if ($Output) {
    [System.IO.Path]::GetFullPath($Output)
} else {
    Join-Path $DataDir "instagram_data.xlsx"
}
$statePath = Join-Path $DataDir ".xlsx_sync_state.json"
$snapshotBefore = Get-CsvSnapshot -Directory $DataDir
$fingerprintBefore = Get-SnapshotFingerprint -Snapshot $snapshotBefore

$destination = $requestedWorkbook
if (Test-FileLocked -Path $destination) {
    $destination = if ($Output) {
        $directory = Split-Path -Parent $requestedWorkbook
        $name = [System.IO.Path]::GetFileNameWithoutExtension($requestedWorkbook)
        Join-Path $directory "${name}_updated.xlsx"
    } else {
        Join-Path $DataDir "instagram_data_updated.xlsx"
    }
    Write-Warning "The requested XLSX is open or locked. The synchronized workbook will be saved to: $destination"
    if (Test-FileLocked -Path $destination) {
        $directory = Split-Path -Parent $destination
        $name = [System.IO.Path]::GetFileNameWithoutExtension($destination)
        $stamp = [DateTime]::Now.ToString("yyyyMMdd_HHmmss")
        $destination = Join-Path $directory "${name}_${stamp}.xlsx"
        Write-Warning "The fallback XLSX is also locked. A timestamped workbook will be saved to: $destination"
    }
}

$previousState = $null
if (Test-Path -LiteralPath $statePath -PathType Leaf) {
    try {
        $previousState = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        Write-Warning "The previous XLSX synchronization state could not be read. A full rebuild will run."
    }
}

$destinationIsRequestedWorkbook = [string]::Equals(
    [System.IO.Path]::GetFullPath($destination),
    [System.IO.Path]::GetFullPath($requestedWorkbook),
    [System.StringComparison]::OrdinalIgnoreCase
)
$promotionPending = $destinationIsRequestedWorkbook -and $previousState -and $previousState.pending_promotion
if ($promotionPending) {
    Write-Output "A fallback XLSX is waiting to be reflected. Rebuilding the original workbook from the latest CSV files."
}

if (-not $Force -and $previousState) {
    try {
        $sameDestination = [string]::Equals(
            [System.IO.Path]::GetFullPath([string]$previousState.output),
            [System.IO.Path]::GetFullPath($destination),
            [System.StringComparison]::OrdinalIgnoreCase
        )
        $xlsxIsValid = Test-XlsxPackage -Path $destination
        $xlsxHashMatches = $xlsxIsValid -and
            $previousState.xlsx_sha256 -and
            ((Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant() -eq $previousState.xlsx_sha256)
        if (
            $sameDestination -and
            $previousState.csv_fingerprint -eq $fingerprintBefore -and
            $xlsxHashMatches
        ) {
            Write-Output "XLSX is already synchronized: $destination"
            return
        }
    } catch {
        Write-Warning "The previous synchronization state could not be compared. A full rebuild will run."
    }
}

$destinationDirectory = Split-Path -Parent $destination
if ($destinationDirectory) {
    New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
}

& $pythonExecutable $collectorScript "--data-dir" $DataDir "xlsx" "--output" $destination
$exportExitCode = $LASTEXITCODE
if ($exportExitCode -ne 0) {
    throw "XLSX export failed with exit code $exportExitCode. The CSV files were not changed."
}
if (-not (Test-XlsxPackage -Path $destination)) {
    throw "The generated XLSX package failed validation: $destination"
}

$snapshotAfter = Get-CsvSnapshot -Directory $DataDir
$fingerprintAfter = Get-SnapshotFingerprint -Snapshot $snapshotAfter
if ($fingerprintAfter -ne $fingerprintBefore) {
    Write-Warning "CSV files changed while XLSX was being generated. The workbook is valid, but run this command again after collection stops to include the latest rows."
    return
}

$state = [ordered]@{
    synchronized_at = [DateTime]::UtcNow.ToString("O")
    csv_fingerprint = $fingerprintAfter
    output = [System.IO.Path]::GetFullPath($destination)
    requested_output = [System.IO.Path]::GetFullPath($requestedWorkbook)
    pending_promotion = -not $destinationIsRequestedWorkbook
    xlsx_sha256 = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant()
    csv_files = $snapshotAfter
}
$stateJson = ConvertTo-Json -InputObject $state -Depth 8
$temporaryStatePath = Join-Path $DataDir ".xlsx_sync_state.$PID.tmp"
try {
    Set-Content -LiteralPath $temporaryStatePath -Value $stateJson -Encoding UTF8
    Move-Item -LiteralPath $temporaryStatePath -Destination $statePath -Force
} finally {
    if (Test-Path -LiteralPath $temporaryStatePath) {
        Remove-Item -LiteralPath $temporaryStatePath -Force
    }
}

Write-Output "CSV and XLSX synchronization completed: $destination"
if ($promotionPending) {
    Write-Output "The fallback XLSX result has been reflected in the original workbook: $requestedWorkbook"
}

if (-not $destinationIsRequestedWorkbook) {
    Write-Warning "The original workbook is still locked. The fallback result is marked as pending promotion."
    if ($WaitForUnlockMinutes -gt 0) {
        $deadline = [DateTime]::UtcNow.AddMinutes($WaitForUnlockMinutes)
        Write-Output "Waiting up to $WaitForUnlockMinutes minute(s) for the original workbook to be unlocked..."
        while ((Test-FileLocked -Path $requestedWorkbook) -and [DateTime]::UtcNow -lt $deadline) {
            Start-Sleep -Seconds $UnlockPollSeconds
        }
        if (Test-FileLocked -Path $requestedWorkbook) {
            Write-Warning "The original workbook remained locked. Run sync_xlsx.ps1 again after closing Excel."
        } else {
            Write-Output "The original workbook is unlocked. Applying the latest CSV data now."
            & $PSCommandPath `
                -DataDir $DataDir `
                -Output $requestedWorkbook `
                -Force `
                -WaitForUnlockMinutes 0 `
                -UnlockPollSeconds $UnlockPollSeconds
        }
    } else {
        Write-Output "After closing Excel, run sync_xlsx.ps1 again to update the original workbook."
    }
}
