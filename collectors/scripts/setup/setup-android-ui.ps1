$ErrorActionPreference = 'Stop'
$runtime = Join-Path (Split-Path (Split-Path $PSScriptRoot)) 'scripts\android\tools\android-ui'
$node = Get-Command node.exe -ErrorAction SilentlyContinue
$npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $node -or -not $npm) { throw 'Install Node.js 24+, reopen PowerShell, then run this script again.' }
$nodeVersion = (& $node.Source --version).Trim().TrimStart('v')
if ([version]$nodeVersion -lt [version]'24.0.0') { throw 'Node.js 24+ is required for the Android UI runtime.' }
$savedPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = 'Continue'
    & $npm.Source ci --prefix $runtime --no-audit --no-fund 2>&1 | ForEach-Object { Write-Host $_ }
    $installExitCode = $LASTEXITCODE
}
finally { $ErrorActionPreference = $savedPreference }
if ($installExitCode -ne 0) { throw 'Android UI runtime installation failed. See the npm output above.' }
Write-Host 'Android UI runtime installed. The collector starts its local server automatically.'
