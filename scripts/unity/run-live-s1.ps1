<#
.SYNOPSIS
Runs the live S1 loop smoke test: the production turn loop on a wired Unity game, in real time.

.DESCRIPTION
Hosts the bridge in a PlayMode test (the same host as the bench recorder) and starts
`playtest.bench.live` against it. The model call needs BASETEN_API_KEY in this session's environment;
the script checks it by name only and never prints it. Give it an approval file that names the call
cap: no paid call starts without one. The trace, plan and per-call ledger are written under
-OutputDirectory, which must be new and outside the Unity project. -Frontier is opt-in: it also
needs META_API_KEY and ffmpeg, and its approval file needs the "muse-spark-1.3" arm next to
"mammoth-1". The switch is restored when the script exits.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ProjectPath,
    [Parameter(Mandatory = $true)][string]$EntityPath,
    [Parameter(Mandatory = $true)][string]$CalibrationPath,
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [Parameter(Mandatory = $true)][string]$ApprovalPath,
    [int]$Episodes = 3,
    [int]$MaxTurns = 16,
    [int]$Seed = 2410,
    [int]$TimeoutSeconds = 600,
    [switch]$Frontier,
    [string]$UnityPath
)

$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$project = [IO.Path]::GetFullPath($ProjectPath).TrimEnd([IO.Path]::DirectorySeparatorChar)
$entity = [IO.Path]::GetFullPath($EntityPath)
$calibration = [IO.Path]::GetFullPath($CalibrationPath)
$approval = [IO.Path]::GetFullPath($ApprovalPath)
$output = [IO.Path]::GetFullPath($OutputDirectory).TrimEnd([IO.Path]::DirectorySeparatorChar)
foreach ($required in @($entity, $calibration, $approval)) {
    if (-not [IO.File]::Exists($required)) { throw "Missing input file: $required" }
}
if ($output.Equals($project, [StringComparison]::OrdinalIgnoreCase) -or
    $output.StartsWith($project + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The output directory must be outside the Unity project.'
}
if ([IO.Directory]::Exists($output)) { throw 'Choose a new output directory; existing evidence is never overwritten.' }
if ([string]::IsNullOrEmpty($env:BASETEN_API_KEY)) { throw 'BASETEN_API_KEY is not set in this session.' }
if ($Frontier) {
    if ([string]::IsNullOrEmpty($env:META_API_KEY)) { throw 'META_API_KEY is not set in this session.' }
    if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { throw 'ffmpeg is not on PATH.' }
}
$manifest = Get-Content -LiteralPath (Join-Path $project 'Packages/manifest.json') -Raw | ConvertFrom-Json
$expected = 'file:' + (Join-Path $repoRoot 'bridge').Replace('\', '/')
if (-not [string]::Equals($manifest.dependencies.'com.playtest.bridge', $expected, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The game is not wired to this checkout. Run scripts/unity/wire-fpskit.ps1 first.'
}
$python = (& uv run --directory $repoRoot python -c 'import sys; print(sys.executable)')
if ($LASTEXITCODE -ne 0 -or -not [IO.File]::Exists($python)) { throw 'Could not locate the repository Python executable.' }
New-Item -ItemType Directory -Path $output | Out-Null
$names = @('UPA_BENCH_OUTPUT', 'UPA_BENCH_PYTHON', 'UPA_BENCH_ENTITY', 'UPA_BENCH_CALIBRATION', 'UPA_BENCH_MODULE',
           'UPA_BENCH_TIMEOUT_S', 'UPA_LIVE_APPROVAL', 'UPA_LIVE_EPISODES', 'UPA_LIVE_MAX_TURNS', 'UPA_LIVE_SEED',
           'UPA_LIVE_FRONTIER')
$previous = @{}
foreach ($name in $names) { $previous[$name] = [Environment]::GetEnvironmentVariable($name) }
try {
    $env:UPA_BENCH_OUTPUT = Join-Path $output 'trace.json'
    $env:UPA_BENCH_PYTHON = $python
    $env:UPA_BENCH_ENTITY = $entity
    $env:UPA_BENCH_CALIBRATION = $calibration
    $env:UPA_BENCH_MODULE = 'playtest.bench.live'
    $env:UPA_BENCH_TIMEOUT_S = [string]$TimeoutSeconds
    $env:UPA_LIVE_APPROVAL = $approval
    $env:UPA_LIVE_EPISODES = [string]$Episodes
    $env:UPA_LIVE_MAX_TURNS = [string]$MaxTurns
    $env:UPA_LIVE_SEED = [string]$Seed
    $env:UPA_LIVE_FRONTIER = if ($Frontier) { '1' } else { '0' }
    $runner = Join-Path $PSScriptRoot 'run-editmode-tests.ps1'
    $runnerArgs = @('-ExecutionPolicy', 'Bypass', '-File', $runner, '-ProjectPath', $project,
                    '-TestPlatform', 'PlayMode', '-TestFilter', 'Playtest.Bridge.Tests.BenchRecordingPlayModeTests',
                    '-ResultsPath', (Join-Path $output 'playmode-results.xml'), '-LogPath', (Join-Path $output 'playmode.log'))
    if ($UnityPath) { $runnerArgs += @('-UnityPath', $UnityPath) }
    & powershell.exe @runnerArgs
    if ($LASTEXITCODE -ne 0) { throw "The live run failed (exit $LASTEXITCODE); inspect the retained trace and log." }
    Write-Host "Saved trace: $env:UPA_BENCH_OUTPUT"
}
finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $previous[$name]) }
}
