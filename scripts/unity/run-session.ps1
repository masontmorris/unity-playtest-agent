<#
.SYNOPSIS
Runs S1 in the production turn loop on a wired Unity game, driven by a preflight manifest.

.DESCRIPTION
Hosts the bridge in the PlayMode bench host (`BenchRecordingPlayModeTests`, the same host as
`run-live-s1.ps1`) and starts `playtest.runner.cli` against it. The host registers one bridge action per
keyboard key under the names the manifest's offered controls carry, so every executable manifest action
runs under its own control and its own hold. The model call needs BASETEN_API_KEY in this session's
environment; the script checks it by name only and never prints it.

No paid call starts without an approval file and a call cap (-MaxCalls). Every call reserves its row in
`<OutputDirectory>/calls` before it is sent. The output directory must be new and outside the Unity
project, and the game must be wired to this checkout (`wire-fpskit.ps1`). The run summary is
`<OutputDirectory>/run.json`; the trace is `trace.jsonl` beside it. Run one session at a time: only one
Editor may own a game project, and the bridge test runner serializes them.

The host stops a client after -WallLimitSeconds plus a margin, and accepts at most 1800 seconds, so the
wall limit is capped at 1500. Every environment variable the script sets is restored when it exits.

S1's calls are paced to -TokensPerMinute (default 85000, which leaves 15% headroom under the provider's
100,000): the runner waits before a turn when the last minute's tokens plus the next call's would pass it.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ProjectPath,
    [Parameter(Mandatory = $true)][string]$EntityPath,
    [Parameter(Mandatory = $true)][string]$CalibrationPath,
    [Parameter(Mandatory = $true)][string]$ManifestPath,
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [Parameter(Mandatory = $true)][string]$ApprovalPath,
    [Parameter(Mandatory = $true)][int]$MaxCalls,
    [int]$WallLimitSeconds = 600,
    [double]$MaxAgeSeconds = 0.5,
    [int]$MaxProviderFailures = 3,
    [int]$TokensPerMinute = 85000,
    [string]$Objective,
    [string]$ObjectivePath,
    [string]$UnityPath
)

$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$project = [IO.Path]::GetFullPath($ProjectPath).TrimEnd([IO.Path]::DirectorySeparatorChar)
$entity = [IO.Path]::GetFullPath($EntityPath)
$calibration = [IO.Path]::GetFullPath($CalibrationPath)
$manifestFile = [IO.Path]::GetFullPath($ManifestPath)
$approval = [IO.Path]::GetFullPath($ApprovalPath)
$output = [IO.Path]::GetFullPath($OutputDirectory).TrimEnd([IO.Path]::DirectorySeparatorChar)
foreach ($required in @($entity, $calibration, $manifestFile, $approval)) {
    if (-not [IO.File]::Exists($required)) { throw "Missing input file: $required" }
}
if ($MaxCalls -lt 1) { throw '-MaxCalls must be at least 1: no paid call starts without a call cap.' }
if ($WallLimitSeconds -lt 30 -or $WallLimitSeconds -gt 1500) { throw '-WallLimitSeconds must be between 30 and 1500.' }
if ($MaxAgeSeconds -le 0) { throw '-MaxAgeSeconds must be positive.' }
if ($MaxProviderFailures -lt 1) { throw '-MaxProviderFailures must be at least 1.' }
if ($TokensPerMinute -lt 1) { throw '-TokensPerMinute must be at least 1.' }
if ($Objective -and $ObjectivePath) { throw 'Give -Objective or -ObjectivePath, not both.' }
if ($ObjectivePath -and -not [IO.File]::Exists([IO.Path]::GetFullPath($ObjectivePath))) { throw "Missing objective file: $ObjectivePath" }
if ($output.Equals($project, [StringComparison]::OrdinalIgnoreCase) -or
    $output.StartsWith($project + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The output directory must be outside the Unity project.'
}
if ([IO.Directory]::Exists($output)) { throw 'Choose a new output directory; existing evidence is never overwritten.' }
if ([string]::IsNullOrEmpty($env:BASETEN_API_KEY)) { throw 'BASETEN_API_KEY is not set in this session.' }
$packages = Get-Content -LiteralPath (Join-Path $project 'Packages/manifest.json') -Raw | ConvertFrom-Json
$expected = 'file:' + (Join-Path $repoRoot 'bridge').Replace('\', '/')
if (-not [string]::Equals($packages.dependencies.'com.playtest.bridge', $expected, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The game is not wired to this checkout. Run scripts/unity/wire-fpskit.ps1 first.'
}
$python = (& uv run --directory $repoRoot python -c 'import sys; print(sys.executable)')
if ($LASTEXITCODE -ne 0 -or -not [IO.File]::Exists($python)) { throw 'Could not locate the repository Python executable.' }
New-Item -ItemType Directory -Path $output | Out-Null
$objectiveFile = $null
if ($ObjectivePath) { $objectiveFile = [IO.Path]::GetFullPath($ObjectivePath) }
elseif ($Objective) {
    $objectiveFile = Join-Path $output 'objective.txt'
    [IO.File]::WriteAllText($objectiveFile, $Objective, (New-Object Text.UTF8Encoding($false)))
}
$invariant = [Globalization.CultureInfo]::InvariantCulture
$names = @('UPA_BENCH_OUTPUT', 'UPA_BENCH_PYTHON', 'UPA_BENCH_ENTITY', 'UPA_BENCH_CALIBRATION', 'UPA_BENCH_MODULE',
           'UPA_BENCH_TIMEOUT_S', 'UPA_RUN_MANIFEST', 'UPA_RUN_APPROVAL', 'UPA_RUN_MAX_CALLS', 'UPA_RUN_WALL_LIMIT_S',
           'UPA_RUN_MAX_AGE_S', 'UPA_RUN_MAX_PROVIDER_FAILURES', 'UPA_RUN_TOKENS_PER_MINUTE', 'UPA_RUN_OBJECTIVE_FILE')
$previous = @{}
foreach ($name in $names) { $previous[$name] = [Environment]::GetEnvironmentVariable($name) }
try {
    $env:UPA_BENCH_OUTPUT = Join-Path $output 'run.json'
    $env:UPA_BENCH_PYTHON = $python
    $env:UPA_BENCH_ENTITY = $entity
    $env:UPA_BENCH_CALIBRATION = $calibration
    $env:UPA_BENCH_MODULE = 'playtest.runner.cli'
    $env:UPA_BENCH_TIMEOUT_S = [string]($WallLimitSeconds + 180)
    $env:UPA_RUN_MANIFEST = $manifestFile
    $env:UPA_RUN_APPROVAL = $approval
    $env:UPA_RUN_MAX_CALLS = [string]$MaxCalls
    $env:UPA_RUN_WALL_LIMIT_S = [string]$WallLimitSeconds
    $env:UPA_RUN_MAX_AGE_S = $MaxAgeSeconds.ToString($invariant)
    $env:UPA_RUN_MAX_PROVIDER_FAILURES = [string]$MaxProviderFailures
    $env:UPA_RUN_TOKENS_PER_MINUTE = [string]$TokensPerMinute
    $env:UPA_RUN_OBJECTIVE_FILE = if ($objectiveFile) { $objectiveFile } else { '' }
    $runner = Join-Path $PSScriptRoot 'run-editmode-tests.ps1'
    $runnerArgs = @('-ExecutionPolicy', 'Bypass', '-File', $runner, '-ProjectPath', $project,
                    '-TestPlatform', 'PlayMode', '-TestFilter', 'Playtest.Bridge.Tests.BenchRecordingPlayModeTests',
                    '-ResultsPath', (Join-Path $output 'playmode-results.xml'), '-LogPath', (Join-Path $output 'playmode.log'))
    if ($UnityPath) { $runnerArgs += @('-UnityPath', $UnityPath) }
    & powershell.exe @runnerArgs
    if ($LASTEXITCODE -ne 0) { throw "The session failed (exit $LASTEXITCODE); inspect the retained summary, trace and log." }
    Write-Host "Saved run summary: $env:UPA_BENCH_OUTPUT"
    Write-Host "Saved trace: $(Join-Path $output 'trace.jsonl')"
}
finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $previous[$name]) }
}
