<##
.SYNOPSIS
Recollects Reel URLs from selected rows of an Excel workbook.

.EXAMPLE
.\refresh.ps1 -InputXlsx .\data_web\fashion_reels.xlsx -StartRow 10 -EndRow 13 -Background
#>

[CmdletBinding(PositionalBinding = $false)]
param(
    [Alias('AnalysisFile', 'File')]
    [string]$InputXlsx,
    [ValidateRange(1, 2147483647)]
    [int]$StartRow,
    [ValidateRange(1, 2147483647)]
    [int]$EndRow,
    [string]$DataDir,
    [switch]$Background,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CollectorArguments
)

if ($PSBoundParameters.ContainsKey('StartRow') -and $PSBoundParameters.ContainsKey('EndRow') -and $EndRow -lt $StartRow) {
    throw '-EndRow must be greater than or equal to -StartRow.'
}

$refreshArguments = @('refresh')
if ($PSBoundParameters.ContainsKey('InputXlsx')) {
    $refreshArguments += @('--input-xlsx', $InputXlsx)
}
if ($PSBoundParameters.ContainsKey('StartRow')) {
    $refreshArguments += @('--start-row', [string]$StartRow)
}
if ($PSBoundParameters.ContainsKey('EndRow')) {
    $refreshArguments += @('--end-row', [string]$EndRow)
}
if ($PSBoundParameters.ContainsKey('DataDir')) {
    $refreshArguments += @('--data-dir', $DataDir)
}
if ($Background) {
    $refreshArguments += '--background'
}
if ($CollectorArguments) {
    $refreshArguments += $CollectorArguments
}

& (Join-Path $PSScriptRoot 'collector.ps1') @refreshArguments
exit $LASTEXITCODE
