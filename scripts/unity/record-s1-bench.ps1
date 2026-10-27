[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ProjectPath,
    [Parameter(Mandatory = $true)][string]$EntityPath,
    [Parameter(Mandatory = $true)][string]$CalibrationPath,
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [string]$UnityPath
)

$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$project = [IO.Path]::GetFullPath($ProjectPath).TrimEnd([IO.Path]::DirectorySeparatorChar)
$entity = [IO.Path]::GetFullPath($EntityPath)
$calibration = [IO.Path]::GetFullPath($CalibrationPath)
$output = [IO.Path]::GetFullPath($OutputDirectory).TrimEnd([IO.Path]::DirectorySeparatorChar)
if (-not [IO.File]::Exists($entity) -or -not [IO.File]::Exists($calibration)) {
    throw 'The stage 2 entity and calibration records are required.'
}
if ($output.Equals($project, [StringComparison]::OrdinalIgnoreCase) -or
    $output.StartsWith($project + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The output directory must be outside the Unity project.'
}
if ([IO.Directory]::Exists($output)) { throw 'Choose a new output directory; existing evidence is never overwritten.' }
$manifest = Get-Content -LiteralPath (Join-Path $project 'Packages/manifest.json') -Raw | ConvertFrom-Json
$expected = 'file:' + (Join-Path $repoRoot 'bridge').Replace('\', '/')
if (-not [string]::Equals($manifest.dependencies.'com.playtest.bridge', $expected, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The game is not wired to this checkout. Wire this bridge package before recording.'
}
$python = (& uv run --directory $repoRoot python -c 'import sys; print(sys.executable)')
if ($LASTEXITCODE -ne 0 -or -not [IO.File]::Exists($python)) { throw 'Could not locate the repository Python executable.' }
New-Item -ItemType Directory -Path $output | Out-Null
$names = @('UPA_BENCH_OUTPUT', 'UPA_BENCH_PYTHON', 'UPA_BENCH_ENTITY', 'UPA_BENCH_CALIBRATION')
$previous = @{}
foreach ($name in $names) { $previous[$name] = [Environment]::GetEnvironmentVariable($name) }
try {
    $env:UPA_BENCH_OUTPUT = Join-Path $output 'frames.json'
    $env:UPA_BENCH_PYTHON = $python
    $env:UPA_BENCH_ENTITY = $entity
    $env:UPA_BENCH_CALIBRATION = $calibration
    $runner = Join-Path $PSScriptRoot 'run-editmode-tests.ps1'
    $runnerArgs = @('-ExecutionPolicy', 'Bypass', '-File', $runner, '-ProjectPath', $project,
                    '-TestPlatform', 'PlayMode', '-TestFilter', 'Playtest.Bridge.Tests.BenchRecordingPlayModeTests',
                    '-ResultsPath', (Join-Path $output 'playmode-results.xml'), '-LogPath', (Join-Path $output 'playmode.log'))
    if ($UnityPath) { $runnerArgs += @('-UnityPath', $UnityPath) }
    & powershell.exe @runnerArgs
    if ($LASTEXITCODE -ne 0) { throw "The recording run failed (exit $LASTEXITCODE); inspect the retained output and log." }
    Write-Host "Saved recording: $env:UPA_BENCH_OUTPUT"
}
finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $previous[$name]) }
}
