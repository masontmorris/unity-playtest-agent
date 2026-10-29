[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ProjectPath,
    [Parameter(Mandatory = $true)][string]$PlanPath,
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [string]$UnityPath
)

# Reproduce one logged runtime error k of n times, each time after a scene reload, for the one keyboard
# control the caller's plan names. The existing bridge-test runner supplies the single-Editor mutex and
# refuses to start while the game is open in an Editor. This script restores every environment variable
# it sets and never writes to the game project; all evidence stays in the new output directory:
#   repro.json       the report (written by the client, even when the run fails part-way)
#   plan.json        a byte-for-byte copy of the plan that was run
#   handshake/       the numbered reload requests and completions
#   provenance.json  the game's git HEAD and status before and after the run
#   unity-playmode.log, unity-playmode-results.xml   copies of the runner's Unity log and results
$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$project = [IO.Path]::GetFullPath($ProjectPath).TrimEnd([IO.Path]::DirectorySeparatorChar)
$plan = [IO.Path]::GetFullPath($PlanPath)
$output = [IO.Path]::GetFullPath($OutputDirectory).TrimEnd([IO.Path]::DirectorySeparatorChar)
if (-not [IO.File]::Exists($plan)) { throw 'The reproduction plan does not exist.' }
if ($output.Equals($project, [StringComparison]::OrdinalIgnoreCase) -or
    $output.StartsWith($project + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The reproduction output must be outside the Unity project.'
}
if ([IO.File]::Exists($output)) { throw "The output path is a file: $output" }
if ([IO.Directory]::Exists($output) -and @(Get-ChildItem -LiteralPath $output -Force).Count -gt 0) {
    throw "The output directory is not empty; choose a new output directory: $output"
}
$manifestJson = Get-Content -LiteralPath (Join-Path $project 'Packages/manifest.json') -Raw | ConvertFrom-Json
$expected = 'file:' + (Join-Path $repoRoot 'bridge').Replace('\', '/')
if (-not [string]::Equals($manifestJson.dependencies.'com.playtest.bridge', $expected, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The game is not wired to this checkout. Wire this bridge package before reproducing.'
}
if ([IO.File]::Exists((Join-Path $project 'Temp/UnityLockfile'))) {
    throw 'The project looks open in a Unity Editor (Temp/UnityLockfile exists). Close that Editor first.'
}
$python = (& uv run --directory $repoRoot python -c 'import sys; print(sys.executable)')
if ($LASTEXITCODE -ne 0 -or -not [IO.File]::Exists($python)) { throw 'Could not locate the repository Python executable.' }

# Reject a bad plan now, before Unity is started. The client validates it again and records its SHA-256.
$check = @'
import sys
from pathlib import Path
from playtest.preflight import repro
try:
    print(repro.read_plan(Path(sys.argv[1]))[1])
except (OSError, ValueError) as exc:
    sys.exit('The reproduction plan was rejected: ' + str(exc))
'@
$planHash = (& $python -c $check $plan)
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($planHash)) { throw 'The reproduction plan was rejected; see above.' }
$planHash = ([string]$planHash).Trim()

$planCopy = Join-Path $output 'plan.json'
$report = Join-Path $output 'repro.json'
$handshake = Join-Path $output 'handshake'
$provenancePath = Join-Path $output 'provenance.json'
New-Item -ItemType Directory -Force -Path $output | Out-Null
Copy-Item -LiteralPath $plan -Destination $planCopy
$copyHash = (Get-FileHash -LiteralPath $planCopy -Algorithm SHA256).Hash.ToLowerInvariant()
if ($copyHash -ne $planHash) { throw 'The plan copy does not match the plan that was validated.' }

function Get-GitState {
    param([string]$Path)
    $head = $null
    $status = $null
    $problem = $null
    try {
        $headLines = @(& git -C $Path rev-parse HEAD)
        if ($LASTEXITCODE -ne 0) { throw "git rev-parse HEAD failed (exit $LASTEXITCODE)" }
        $statusLines = @(& git -C $Path status --porcelain)
        if ($LASTEXITCODE -ne 0) { throw "git status --porcelain failed (exit $LASTEXITCODE)" }
        $head = [string]$headLines[0]
        $status = @($statusLines | ForEach-Object { [string]$_ })
    }
    catch { $problem = $_.Exception.Message }
    # Missing evidence stays null; an unreadable repository is never reported as clean.
    return [ordered]@{ captured_utc = (Get-Date).ToUniversalTime().ToString('o'); head = $head;
                       status_porcelain = $status; error = $problem }
}

function Write-Provenance {
    param($Value)
    $json = $Value | ConvertTo-Json -Depth 6
    [IO.File]::WriteAllText($provenancePath, $json + "`n", (New-Object System.Text.UTF8Encoding($false)))
}

$provenance = [ordered]@{
    schema_version = 1
    plan_sha256 = $planHash
    plan_copy = $planCopy
    project = $project
    bridge_package = $expected
    harness = Get-GitState $repoRoot
    game_before = Get-GitState $project
    game_after = $null
    run_exit_code = $null
    started_utc = (Get-Date).ToUniversalTime().ToString('o')
    finished_utc = $null
}
Write-Provenance $provenance

$runner = Join-Path $PSScriptRoot 'run-editmode-tests.ps1'
$names = @('UPA_REPRO_PLAN', 'UPA_REPRO_OUTPUT', 'UPA_REPRO_PYTHON', 'UPA_REPRO_HANDSHAKE_DIR')
$previous = @{}
foreach ($name in $names) { $previous[$name] = [Environment]::GetEnvironmentVariable($name) }
try {
    $env:UPA_REPRO_PLAN = $planCopy
    $env:UPA_REPRO_OUTPUT = $report
    $env:UPA_REPRO_PYTHON = $python
    $env:UPA_REPRO_HANDSHAKE_DIR = $handshake
    $runnerArgs = @('-ExecutionPolicy', 'Bypass', '-File', $runner, '-ProjectPath', $project,
                    '-TestPlatform', 'PlayMode', '-TestFilter', 'Playtest.Bridge.Tests.ReproPlayModeTests')
    if ($UnityPath) { $runnerArgs += @('-UnityPath', $UnityPath) }
    & powershell.exe @runnerArgs
    $provenance.run_exit_code = $LASTEXITCODE
    if ($LASTEXITCODE -ne 0) { throw "The reproduction run failed (exit $LASTEXITCODE). Review the Unity log and any saved report." }
    if (-not [IO.File]::Exists($report)) { throw 'The reproduction run reported success but wrote no report.' }
    $saved = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
    Write-Host "Saved reproduction report: $report"
    Write-Host ("Status: {0}  attempted={1} valid={2} k={3} n={4}" -f $saved.status, $saved.summary.attempted,
                $saved.summary.valid, $saved.summary.k, $saved.summary.n)
}
finally {
    $provenance.game_after = Get-GitState $project
    $provenance.finished_utc = (Get-Date).ToUniversalTime().ToString('o')
    Write-Provenance $provenance
    # The runner replaces its log and results on the next run, so keep this run's copies with its evidence.
    $unityRuns = Join-Path (Join-Path $repoRoot 'runs/bridge-tests') (Split-Path $project -Leaf)
    foreach ($pair in @(@('playmode.log', 'unity-playmode.log'), @('playmode-results.xml', 'unity-playmode-results.xml'))) {
        $source = Join-Path $unityRuns $pair[0]
        if ([IO.File]::Exists($source)) {
            try { Copy-Item -LiteralPath $source -Destination (Join-Path $output $pair[1]) }
            catch { Write-Warning "Could not keep a copy of $($pair[0]): $($_.Exception.Message)" }
        }
    }
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $previous[$name]) }
}
