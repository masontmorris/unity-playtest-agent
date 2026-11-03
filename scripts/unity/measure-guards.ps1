<#
.SYNOPSIS
Measure the guard table in N fresh sessions and score them: a finding's reproduction from a clean start, k of n.

.DESCRIPTION
Each session is a fresh Play Mode start (run-session.ps1 -MeasureGuards -GuardsOnly): the guard table is measured
before play and the session ends with no S1 call, so a session takes about a minute and costs nothing. The sessions
run one after another (one Unity Editor per project). With -ExpectationsPath, the expectations are then scored
against every session's table (`playtest.runner.expectations score`): an expectation not met in more than half of
them is a mismatch, reproduced k of n from a clean start. Without it, the tables' asymmetries are tallied.

The runner still requires an approval file and a call cap; none of the cap is spent. Credentials are not read here.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ProjectPath,
    [Parameter(Mandatory = $true)][string]$EntityPath,
    [Parameter(Mandatory = $true)][string]$CalibrationPath,
    [Parameter(Mandatory = $true)][string]$ManifestPath,
    [Parameter(Mandatory = $true)][string]$ApprovalPath,
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [ValidateRange(1, 20)][int]$Sessions = 3,
    [string]$DiscoveryPath,
    [string]$ScenePath,
    [string]$ExpectationsPath,
    [string]$ManifestOrigin = 'guard table reproduction',
    [switch]$PointMoves
)

$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$output = [IO.Path]::GetFullPath($OutputDirectory)
if ([IO.Directory]::Exists($output)) { throw "The output directory already exists: $output" }
New-Item -ItemType Directory -Path $output | Out-Null
$runner = Join-Path $PSScriptRoot 'run-session.ps1'
$runs = @()
for ($i = 1; $i -le $Sessions; $i++) {
    $run = Join-Path $output ('{0:d2}' -f $i)
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $runner,
        '-ProjectPath', $ProjectPath, '-EntityPath', $EntityPath, '-CalibrationPath', $CalibrationPath,
        '-ManifestPath', $ManifestPath, '-ApprovalPath', $ApprovalPath, '-OutputDirectory', $run,
        '-MaxCalls', '1', '-WallLimitSeconds', '60', '-ManifestOrigin', "$ManifestOrigin; session $i of $Sessions",
        '-Objective', 'measure the guard table', '-MeasureGuards', '-GuardsOnly', '-NoFinish')
    if ($DiscoveryPath) { $arguments += @('-DiscoveryPath', $DiscoveryPath) }
    if ($ScenePath) { $arguments += @('-ScenePath', $ScenePath) }
    if ($ExpectationsPath) { $arguments += @('-ExpectationsPath', $ExpectationsPath) }
    if ($PointMoves) { $arguments += '-PointMoves' }
    & pwsh @arguments *> (Join-Path $output ('{0:d2}.log' -f $i))
    $measured = [IO.File]::Exists((Join-Path $run 'guards.json'))
    Write-Host ("session {0} of {1}: exit {2}, guards.json {3}" -f $i, $Sessions, $LASTEXITCODE, $(if ($measured) { 'yes' } else { 'missing' }))
    if ($measured) { $runs += $run }
}
if (-not $runs) { throw 'No session measured a guard table.' }
$python = (& uv run --directory $repoRoot python -c 'import sys; print(sys.executable)')
if ($ExpectationsPath) {
    & $python -m playtest.runner.expectations score --expectations $ExpectationsPath --runs @runs --output (Join-Path $output 'score.json')
} else {
    & $python -m playtest.runner.guards tally --runs @runs --manifest $ManifestPath --output (Join-Path $output 'tally.json')
}
if ($LASTEXITCODE -ne 0) { throw "Scoring failed (exit $LASTEXITCODE)." }
Write-Host "Measured $($runs.Count) of $Sessions sessions into $output"
