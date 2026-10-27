[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ProjectPath,
    [Parameter(Mandatory = $true)][string]$EntityPath,
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [string]$UnityPath
)

$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$project = [IO.Path]::GetFullPath($ProjectPath).TrimEnd([IO.Path]::DirectorySeparatorChar)
$entity = [IO.Path]::GetFullPath($EntityPath)
$output = [IO.Path]::GetFullPath($OutputDirectory).TrimEnd([IO.Path]::DirectorySeparatorChar)
if (-not [IO.File]::Exists($entity)) { throw 'A stage 2 entity record is required.' }
if ($output.Equals($project, [StringComparison]::OrdinalIgnoreCase) -or
    $output.StartsWith($project + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Calibration output must be outside the Unity project.'
}
$calibration = Join-Path $output 'calibration.json'
$reset = Join-Path $output 'reset.json'
$record = Join-Path $output 'record.json'
if ([IO.File]::Exists($calibration) -or [IO.File]::Exists($reset) -or [IO.File]::Exists($record)) {
    throw 'A calibration output already exists; choose a new output directory.'
}
$manifest = Get-Content -LiteralPath (Join-Path $project 'Packages/manifest.json') -Raw | ConvertFrom-Json
$expected = 'file:' + (Join-Path $repoRoot 'bridge').Replace('\', '/')
if (-not [string]::Equals($manifest.dependencies.'com.playtest.bridge', $expected, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The game is not wired to this checkout. Wire this bridge package before calibrating.'
}
$python = (& uv run --directory $repoRoot python -c 'import sys; print(sys.executable)')
if ($LASTEXITCODE -ne 0 -or -not [IO.File]::Exists($python)) { throw 'Could not locate the repository Python executable.' }
New-Item -ItemType Directory -Force -Path $output | Out-Null
$runner = Join-Path $PSScriptRoot 'run-editmode-tests.ps1'
$names = @('UPA_CALIBRATION_OUTPUT', 'UPA_CALIBRATION_PYTHON', 'UPA_CALIBRATION_ENTITY', 'UPA_CALIBRATION_RESET_OUTPUT')
$previous = @{}
foreach ($name in $names) { $previous[$name] = [Environment]::GetEnvironmentVariable($name) }
try {
    $env:UPA_CALIBRATION_OUTPUT = $calibration
    $env:UPA_CALIBRATION_PYTHON = $python
    $env:UPA_CALIBRATION_ENTITY = $entity
    $env:UPA_CALIBRATION_RESET_OUTPUT = $reset
    $runnerArgs = @('-ExecutionPolicy', 'Bypass', '-File', $runner, '-ProjectPath', $project,
                    '-TestPlatform', 'PlayMode', '-TestFilter', 'Playtest.Bridge.Tests.CalibrationPlayModeTests')
    if ($UnityPath) { $runnerArgs += @('-UnityPath', $UnityPath) }
    & powershell.exe @runnerArgs
    if ($LASTEXITCODE -ne 0) { throw "The calibration run failed (exit $LASTEXITCODE). Review any saved output and Unity log." }
    & $python -m playtest.preflight.calibration_record --project $project --calibration $calibration --reset $reset --output $record
    if ($LASTEXITCODE -ne 0) { throw 'Could not combine the calibration and reset evidence.' }
    Write-Host "Saved calibration record: $record"
}
finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $previous[$name]) }
}
