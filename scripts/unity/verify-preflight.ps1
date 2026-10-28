[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ProjectPath,
    [Parameter(Mandatory = $true)][string]$ManifestPath,
    [Parameter(Mandatory = $true)][string]$CalibrationPath,
    [Parameter(Mandatory = $true)][string]$Action,
    [Parameter(Mandatory = $true)][string]$OutputPath,
    [string]$UnityPath
)

# One explicitly named manifest action and one independent scene reload. The existing
# bridge-test runner supplies the single-Editor mutex and refuses to start while the
# game is open in an Editor. This script restores every environment variable and never
# writes to the game project; all evidence stays in the caller's output directory.
$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$project = [IO.Path]::GetFullPath($ProjectPath).TrimEnd([IO.Path]::DirectorySeparatorChar)
$manifest = [IO.Path]::GetFullPath($ManifestPath)
$calibration = [IO.Path]::GetFullPath($CalibrationPath)
$output = [IO.Path]::GetFullPath($OutputPath)
if ([string]::IsNullOrWhiteSpace($Action)) { throw 'An explicit manifest action name is required.' }
if (-not [IO.File]::Exists($manifest)) { throw 'The preflight manifest does not exist.' }
if (-not [IO.File]::Exists($calibration)) { throw 'The calibration record does not exist.' }
if ($output.Equals($project, [StringComparison]::OrdinalIgnoreCase) -or
    $output.StartsWith($project + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The verification output must be outside the Unity project.'
}
$request = $output + '.reload-request.json'
$done = $output + '.reload-done.json'
$handshakeFiles = @($output, $request, $done, $request + '.writing', $done + '.writing')
foreach ($path in $handshakeFiles) {
    if ([IO.File]::Exists($path)) { throw "A verification output or handshake file already exists: $path" }
}
$manifestJson = Get-Content -LiteralPath (Join-Path $project 'Packages/manifest.json') -Raw | ConvertFrom-Json
$expected = 'file:' + (Join-Path $repoRoot 'bridge').Replace('\', '/')
if (-not [string]::Equals($manifestJson.dependencies.'com.playtest.bridge', $expected, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The game is not wired to this checkout. Wire this bridge package before verifying.'
}
$python = (& uv run --directory $repoRoot python -c 'import sys; print(sys.executable)')
if ($LASTEXITCODE -ne 0 -or -not [IO.File]::Exists($python)) { throw 'Could not locate the repository Python executable.' }
$outputDirectory = [IO.Path]::GetDirectoryName($output)
if (-not [IO.Directory]::Exists($outputDirectory)) {
    New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null
}
$runner = Join-Path $PSScriptRoot 'run-editmode-tests.ps1'
$names = @('UPA_VERIFY_OUTPUT', 'UPA_VERIFY_PYTHON', 'UPA_VERIFY_MANIFEST', 'UPA_VERIFY_CALIBRATION',
           'UPA_VERIFY_ACTION', 'UPA_VERIFY_RELOAD_REQUEST', 'UPA_VERIFY_RELOAD_DONE')
$previous = @{}
foreach ($name in $names) { $previous[$name] = [Environment]::GetEnvironmentVariable($name) }
try {
    $env:UPA_VERIFY_OUTPUT = $output
    $env:UPA_VERIFY_PYTHON = $python
    $env:UPA_VERIFY_MANIFEST = $manifest
    $env:UPA_VERIFY_CALIBRATION = $calibration
    $env:UPA_VERIFY_ACTION = $Action
    $env:UPA_VERIFY_RELOAD_REQUEST = $request
    $env:UPA_VERIFY_RELOAD_DONE = $done
    $runnerArgs = @('-ExecutionPolicy', 'Bypass', '-File', $runner, '-ProjectPath', $project,
                    '-TestPlatform', 'PlayMode', '-TestFilter', 'Playtest.Bridge.Tests.StageSevenPlayModeTests')
    if ($UnityPath) { $runnerArgs += @('-UnityPath', $UnityPath) }
    & powershell.exe @runnerArgs
    if ($LASTEXITCODE -ne 0) { throw "The stage 7 run failed (exit $LASTEXITCODE). Review the Unity log and any saved report." }
    if (-not [IO.File]::Exists($output)) { throw 'The stage 7 run reported success but wrote no verification report.' }
    Write-Host "Saved stage 7 verification: $output"
}
finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $previous[$name]) }
}
