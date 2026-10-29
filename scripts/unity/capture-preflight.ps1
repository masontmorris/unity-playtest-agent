[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ProjectPath,
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [string]$UnityPath
)

$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$project = [IO.Path]::GetFullPath($ProjectPath).TrimEnd([IO.Path]::DirectorySeparatorChar)
$output = [IO.Path]::GetFullPath($OutputDirectory).TrimEnd([IO.Path]::DirectorySeparatorChar)
$projectPrefix = $project + [IO.Path]::DirectorySeparatorChar
if ($output.Equals($project, [StringComparison]::OrdinalIgnoreCase) -or
    $output.StartsWith($projectPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The output directory must be outside the inspected Unity project.'
}

$manifestPath = Join-Path $project 'Packages/manifest.json'
if (-not [IO.File]::Exists($manifestPath)) { throw 'The Unity project manifest does not exist.' }
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$expected = 'file:' + (Join-Path $repoRoot 'bridge').Replace('\', '/')
$actual = $manifest.dependencies.'com.playtest.bridge'
if (-not [string]::Equals($actual, $expected, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The game is not wired to this checkout. Run scripts/unity/wire-fpskit.ps1 with this checkout as PackagePath first.'
}

$survey = Join-Path $output 'survey.json'
$bridgeDiscovery = Join-Path $output 'discovery-bridge.json'
$discovery = Join-Path $output 'discovery.json'
if ([IO.File]::Exists($survey) -or [IO.File]::Exists($bridgeDiscovery) -or [IO.File]::Exists($discovery)) {
    throw 'Output files already exist; choose a new output directory.'
}
New-Item -ItemType Directory -Force -Path $output | Out-Null
$runner = Join-Path $PSScriptRoot 'run-editmode-tests.ps1'
$common = @('-ExecutionPolicy', 'Bypass', '-File', $runner, '-ProjectPath', $project)
if ($UnityPath) { $common += @('-UnityPath', $UnityPath) }
$previousSurvey = [Environment]::GetEnvironmentVariable('UPA_PREFLIGHT_SURVEY_OUTPUT')
$previousDiscovery = [Environment]::GetEnvironmentVariable('UPA_PREFLIGHT_DISCOVERY_OUTPUT')
try {
    $env:UPA_PREFLIGHT_SURVEY_OUTPUT = $survey
    & powershell.exe @common -TestFilter 'Playtest.Bridge.Tests.UnityInspectionSurveyTests'
    if ($LASTEXITCODE -ne 0) { throw "EditMode inspection failed (exit $LASTEXITCODE)." }
    Remove-Item Env:UPA_PREFLIGHT_SURVEY_OUTPUT -ErrorAction SilentlyContinue
    $env:UPA_PREFLIGHT_DISCOVERY_OUTPUT = $bridgeDiscovery
    & powershell.exe @common -TestPlatform PlayMode -TestFilter 'Playtest.Bridge.Tests.InspectionPlayModeTests'
    if ($LASTEXITCODE -ne 0) { throw "PlayMode inspection failed (exit $LASTEXITCODE)." }
    $python = (& uv run --directory $repoRoot python -c 'import sys; print(sys.executable)')
    if ($LASTEXITCODE -ne 0 -or -not [IO.File]::Exists($python)) { throw 'Could not locate the repository Python executable.' }
    & $python -m playtest.preflight.il --discovery $bridgeDiscovery --output $discovery
    if ($LASTEXITCODE -ne 0) {
        throw ("The IL scan failed (exit $LASTEXITCODE). The bridge snapshot is kept at $bridgeDiscovery; " +
               'run playtest-merge-il on it after fixing the cause.')
    }
    Write-Host "Saved survey:           $survey"
    Write-Host "Saved bridge discovery: $bridgeDiscovery"
    Write-Host "Saved discovery:        $discovery"
    Write-Host 'Review caps and errors before treating the survey or discovery as complete.'
}
finally {
    [Environment]::SetEnvironmentVariable('UPA_PREFLIGHT_SURVEY_OUTPUT', $previousSurvey)
    [Environment]::SetEnvironmentVariable('UPA_PREFLIGHT_DISCOVERY_OUTPUT', $previousDiscovery)
}
