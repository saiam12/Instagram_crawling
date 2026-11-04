<##
.SYNOPSIS
Recollects Reel URLs from selected rows of an Excel workbook.

.EXAMPLE
.\refresh.ps1 --input-xlsx .\collector\reels.xlsx --start-row 10 --end-row 13 --uploaded --background
.\refresh.ps1 --data-dir .\data --collection-count 4 --interval-hours 4 --start-row 2 --end-row 100 --background
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
    [string]$Avd = $env:INSTAGRAM_ANDROID_AVD,
    [ValidateRange(1, 2147483647)]
    [int]$CollectionCount,
    [ValidateScript({ -not [double]::IsNaN($_) -and -not [double]::IsInfinity($_) -and $_ -gt 0 })]
    [double]$IntervalHours = 4,
    [ValidateScript({ -not [double]::IsNaN($_) -and -not [double]::IsInfinity($_) -and $_ -gt 0 })]
    [double]$InitialIntervalHours = 2,
    [ValidateScript({ -not [double]::IsNaN($_) -and -not [double]::IsInfinity($_) -and $_ -gt 0 })]
    [double]$MiddleIntervalHours = 3,
    [ValidateScript({ -not [double]::IsNaN($_) -and -not [double]::IsInfinity($_) -and $_ -gt 0 })]
    [double]$Late1IntervalHours = 6,
    [ValidateScript({ -not [double]::IsNaN($_) -and -not [double]::IsInfinity($_) -and $_ -gt 0 })]
    [double]$Late2IntervalHours = 12,
    [ValidateSet('uploaded', 'first-collected', 'FirstCollected')]
    [string]$AgeBasis = 'uploaded',
    [ValidateSet('uploaded', 'last-collected', 'LastCollected')]
    [string]$ScheduleBasis = 'uploaded',
    [switch]$Background,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CollectorArguments
)

# Match collector.ps1's --kebab-case CLI while preserving legacy PowerShell parameters.
$optionNames = @{
    'input-xlsx'='InputXlsx'; 'start-row'='StartRow'; 'end-row'='EndRow'; 'data-dir'='DataDir'
    'collection-count'='CollectionCount'; 'interval-hours'='IntervalHours'
    'initial-interval-hours'='InitialIntervalHours'; 'middle-interval-hours'='MiddleIntervalHours'
    'late1-interval-hours'='Late1IntervalHours'; 'late2-interval-hours'='Late2IntervalHours'
    'age-basis'='AgeBasis'; 'schedule-basis'='ScheduleBasis'
}
$remainingArguments = @()
$requestedScheduleBases = @()
if ($PSBoundParameters.ContainsKey('ScheduleBasis')) { $requestedScheduleBases += $ScheduleBasis.Replace('-', '').ToLowerInvariant() }
for ($index = 0; $index -lt $CollectorArguments.Count; $index++) {
    $argument = [string]$CollectorArguments[$index]
    if ($argument -eq '--background') { $Background = $true; continue }
    if ($argument -in @('--uploaded', '--last-collected')) {
        $requestedScheduleBases += $argument.Substring(2).Replace('-', '')
        continue
    }
    $parts = $argument -split '=', 2
    $key = $parts[0] -replace '^--', ''
    if ($argument.StartsWith('--') -and $optionNames.ContainsKey($key)) {
        if ($parts.Count -eq 2) { $value = $parts[1] }
        elseif (($index + 1) -lt $CollectorArguments.Count -and -not $CollectorArguments[$index + 1].StartsWith('--')) { $value = $CollectorArguments[++$index] }
        else { throw "$($parts[0]) requires a value." }
        $name = $optionNames[$key]
        if ($name -in @('StartRow', 'EndRow', 'CollectionCount')) {
            $parsedInteger = 0
            if (-not [int]::TryParse($value, [ref]$parsedInteger) -or $parsedInteger -lt 1) { throw "$($parts[0]) requires a positive integer." }
            $value = $parsedInteger
        } elseif ($name.EndsWith('IntervalHours')) {
            $value = [double]::Parse($value, [System.Globalization.CultureInfo]::InvariantCulture)
            if ([double]::IsNaN($value) -or [double]::IsInfinity($value) -or $value -le 0) { throw "$($parts[0]) requires a finite positive number." }
        } elseif ($name -eq 'AgeBasis' -and $value -notin @('uploaded', 'first-collected', 'FirstCollected')) {
            throw '--age-basis requires uploaded or first-collected.'
        } elseif ($name -eq 'ScheduleBasis' -and $value -notin @('uploaded', 'last-collected', 'LastCollected')) {
            throw '--schedule-basis requires uploaded or last-collected.'
        }
        Set-Variable -Name $name -Value $value -ErrorAction Stop
        if ($name -eq 'ScheduleBasis') { $requestedScheduleBases += $ScheduleBasis.Replace('-', '').ToLowerInvariant() }
        $PSBoundParameters[$name] = Get-Variable -Name $name -ValueOnly
    } else { $remainingArguments += $argument }
}
$CollectorArguments = $remainingArguments
$requestedScheduleBases = @($requestedScheduleBases | Select-Object -Unique)
if ($requestedScheduleBases.Count -gt 1) { throw 'Choose only one of --uploaded or --last-collected.' }
if ($requestedScheduleBases.Count -eq 1) { $ScheduleBasis = $(if ($requestedScheduleBases[0] -eq 'lastcollected') { 'last-collected' } else { 'uploaded' }) }
if (-not $PSBoundParameters.ContainsKey('AgeBasis')) {
    $AgeBasis = $(if ($ScheduleBasis.Replace('-', '') -eq 'LastCollected') { 'first-collected' } else { 'uploaded' })
}

if ($PSBoundParameters.ContainsKey('StartRow') -and $PSBoundParameters.ContainsKey('EndRow') -and $EndRow -lt $StartRow) {
    throw '--end-row must be greater than or equal to --start-row.'
}
if ($PSBoundParameters.ContainsKey('IntervalHours') -and $PSBoundParameters.ContainsKey('AgeBasis') -and $AgeBasis.Replace('-', '') -eq 'FirstCollected') {
    throw '--age-basis first-collected requires the age schedule; omit --interval-hours and use the stage interval options.'
}

$refreshArguments = @('refresh', '--inbox')
$refreshArguments += @('--schedule-basis', $(if ($ScheduleBasis.Replace('-', '') -eq 'LastCollected') { 'last-collected' } else { 'uploaded' }))
if ($PSBoundParameters.ContainsKey('CollectionCount')) {
    $refreshArguments += @('--collection-count', [string]$CollectionCount)
}
if ($PSBoundParameters.ContainsKey('IntervalHours')) {
    $refreshArguments += @('--interval-hours', $IntervalHours.ToString([System.Globalization.CultureInfo]::InvariantCulture))
    if (-not $PSBoundParameters.ContainsKey('CollectionCount')) {
        $refreshArguments += @('--collection-count', '4')
    }
} else {
    $refreshArguments += @('--age-schedule',
        '--age-basis', $(if ($AgeBasis.Replace('-', '') -eq 'FirstCollected') { 'first-collected' } else { 'uploaded' }),
        '--initial-interval-hours', $InitialIntervalHours.ToString([System.Globalization.CultureInfo]::InvariantCulture),
        '--middle-interval-hours', $MiddleIntervalHours.ToString([System.Globalization.CultureInfo]::InvariantCulture),
        '--late1-interval-hours', $Late1IntervalHours.ToString([System.Globalization.CultureInfo]::InvariantCulture),
        '--late2-interval-hours', $Late2IntervalHours.ToString([System.Globalization.CultureInfo]::InvariantCulture))
}
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

& (Join-Path $PSScriptRoot 'collector.ps1') -Avd $Avd @refreshArguments
exit $LASTEXITCODE
