[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ProjectPath,
    [Parameter(Mandatory = $true)][string]$OutputPath,
    [string]$UnityPath
)

$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$project = [IO.Path]::GetFullPath($ProjectPath).TrimEnd([IO.Path]::DirectorySeparatorChar)
$output = [IO.Path]::GetFullPath($OutputPath)
if ($output.Equals($project, [StringComparison]::OrdinalIgnoreCase) -or
    $output.StartsWith($project + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The measurement output must be outside the Unity project.'
}
if ([IO.File]::Exists($output)) { throw 'The output already exists; choose a new path.' }
$manifest = Get-Content -LiteralPath (Join-Path $project 'Packages/manifest.json') -Raw | ConvertFrom-Json
$expected = 'file:' + (Join-Path $repoRoot 'bridge').Replace('\', '/')
if (-not [string]::Equals($manifest.dependencies.'com.playtest.bridge', $expected, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The game is not wired to this checkout. Wire this bridge package before running the measurement.'
}
$python = (& uv run --directory $repoRoot python -c 'import sys; print(sys.executable)')
if ($LASTEXITCODE -ne 0 -or -not [IO.File]::Exists($python)) { throw 'Could not locate the repository Python executable.' }
$runner = Join-Path $PSScriptRoot 'run-editmode-tests.ps1'
$previousOutput = [Environment]::GetEnvironmentVariable('UPA_ENTITY_OUTPUT')
$previousPython = [Environment]::GetEnvironmentVariable('UPA_ENTITY_PYTHON')
try {
    $env:UPA_ENTITY_OUTPUT = $output
    $env:UPA_ENTITY_PYTHON = $python
    $runnerArgs = @('-ExecutionPolicy', 'Bypass', '-File', $runner, '-ProjectPath', $project,
                    '-TestPlatform', 'PlayMode', '-TestFilter', 'Playtest.Bridge.Tests.ControlledEntityPlayModeTests')
    if ($UnityPath) { $runnerArgs += @('-UnityPath', $UnityPath) }
    & powershell.exe @runnerArgs
    if ($LASTEXITCODE -ne 0) { throw "The measurement run failed (exit $LASTEXITCODE). Review any saved output and the Unity log." }
    Write-Host "Saved movement evidence: $output"
}
finally {
    [Environment]::SetEnvironmentVariable('UPA_ENTITY_OUTPUT', $previousOutput)
    [Environment]::SetEnvironmentVariable('UPA_ENTITY_PYTHON', $previousPython)
}
