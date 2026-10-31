[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ProjectPath,
    [Parameter(Mandatory = $true)][string]$TracePath,
    [Parameter(Mandatory = $true)][string]$ManifestPath,
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [string]$FindingId,
    [string]$ReproCheckPath,
    [string]$FindingPath,
    [int]$Attempts = 5,
    [double]$SettleSeconds = 3.0,
    [string]$UnityPath
)

# Reproduce a finding k of n times (#38): replay a finished session's recorded inputs, up to the finding's
# evidence turn, after a scene reload each attempt, and score the finding's repro_check on the readers (an
# effect check, #154, on the measured displacement of one action instead). A
# code-driven procedure; no model is called. The existing bridge-test runner supplies the single-Editor mutex
# and refuses to start while the game is open in an Editor. This script restores every environment variable it
# sets and never writes to the game project. Everything lands in the new output directory:
#   plan.json        the inputs to replay, the check, the attempts (made from the trace; no call)
#   catalog.json     the manifest's exported catalog the host serves, pinned by SHA-256
#   reproduce.json   the report: each attempt, k of n, the finding's next state
#   handshake/       the numbered reload requests and completions
#   provenance.json  the game's git HEAD and status before and after the run
$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$project = [IO.Path]::GetFullPath($ProjectPath).TrimEnd([IO.Path]::DirectorySeparatorChar)
$trace = [IO.Path]::GetFullPath($TracePath)
$manifest = [IO.Path]::GetFullPath($ManifestPath)
$output = [IO.Path]::GetFullPath($OutputDirectory).TrimEnd([IO.Path]::DirectorySeparatorChar)
foreach ($required in @($trace, $manifest)) { if (-not [IO.File]::Exists($required)) { throw "Missing input file: $required" } }
if ($output.Equals($project, [StringComparison]::OrdinalIgnoreCase) -or
    $output.StartsWith($project + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The output directory must be outside the Unity project.'
}
if ([IO.Directory]::Exists($output)) { throw 'Choose a new output directory; existing evidence is never overwritten.' }
$packages = Get-Content -LiteralPath (Join-Path $project 'Packages/manifest.json') -Raw | ConvertFrom-Json
$expected = 'file:' + (Join-Path $repoRoot 'bridge').Replace('\', '/')
if (-not [string]::Equals($packages.dependencies.'com.playtest.bridge', $expected, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The game is not wired to this checkout. Run scripts/unity/wire-fpskit.ps1 first.'
}
if ([IO.File]::Exists((Join-Path $project 'Temp/UnityLockfile'))) { throw 'The project looks open in a Unity Editor; close it first.' }
$python = (& uv run --directory $repoRoot python -c 'import sys; print(sys.executable)')
if ($LASTEXITCODE -ne 0 -or -not [IO.File]::Exists($python)) { throw 'Could not locate the repository Python executable.' }
New-Item -ItemType Directory -Path $output | Out-Null
$plan = Join-Path $output 'plan.json'
$planArgs = @('-m', 'playtest.runner.reproduce', 'plan', '--trace', $trace, '--manifest', $manifest, '--output', $plan,
              '--attempts', [string]$Attempts, '--settle-s', $SettleSeconds.ToString([Globalization.CultureInfo]::InvariantCulture))
if ($FindingId) { $planArgs += @('--finding', $FindingId) }
if ($ReproCheckPath) { $planArgs += @('--repro-check', [IO.Path]::GetFullPath($ReproCheckPath)) }
if ($FindingPath) { $planArgs += @('--finding-file', [IO.Path]::GetFullPath($FindingPath)) }
Push-Location $repoRoot
try {
    & $python @planArgs
    if ($LASTEXITCODE -ne 0) { throw 'Could not make the replay plan; see above.' }
    $catalog = Join-Path $output 'catalog.json'
    & $python -m playtest.preflight.catalog --manifest $manifest --output $catalog
    if ($LASTEXITCODE -ne 0 -or -not [IO.File]::Exists($catalog)) { throw 'Could not export the manifest catalog.' }
}
finally { Pop-Location }
$catalogSha = (Get-FileHash -LiteralPath $catalog -Algorithm SHA256).Hash.ToLowerInvariant()
$report = Join-Path $output 'reproduce.json'
$handshake = Join-Path $output 'handshake'
function Get-GitState([string]$Path) {
    $head = $null; $status = $null; $problem = $null
    try {
        $head = [string](@(& git -C $Path rev-parse HEAD)[0]); if ($LASTEXITCODE -ne 0) { throw "git rev-parse failed" }
        $status = @(& git -C $Path status --porcelain | ForEach-Object { [string]$_ }); if ($LASTEXITCODE -ne 0) { throw "git status failed" }
    } catch { $problem = $_.Exception.Message }
    return [ordered]@{ captured_utc = (Get-Date).ToUniversalTime().ToString('o'); head = $head; status_porcelain = $status; error = $problem }
}
$provenance = [ordered]@{ schema_version = 1; trace = $trace; manifest = $manifest; catalog_sha256 = $catalogSha
                          harness = Get-GitState $repoRoot; game_before = Get-GitState $project; game_after = $null; run_exit_code = $null }
$names = @('UPA_REPLAY_PLAN', 'UPA_REPLAY_OUTPUT', 'UPA_REPLAY_PYTHON', 'UPA_REPLAY_HANDSHAKE_DIR', 'UPA_REPLAY_MANIFEST',
           'UPA_SESSION_CATALOG', 'UPA_SESSION_CATALOG_SHA256')
$previous = @{}
foreach ($name in $names) { $previous[$name] = [Environment]::GetEnvironmentVariable($name) }
try {
    $env:UPA_REPLAY_PLAN = $plan
    $env:UPA_REPLAY_OUTPUT = $report
    $env:UPA_REPLAY_PYTHON = $python
    $env:UPA_REPLAY_HANDSHAKE_DIR = $handshake
    $env:UPA_REPLAY_MANIFEST = $manifest
    $env:UPA_SESSION_CATALOG = $catalog
    $env:UPA_SESSION_CATALOG_SHA256 = $catalogSha
    $runner = Join-Path $PSScriptRoot 'run-editmode-tests.ps1'
    $runnerArgs = @('-ExecutionPolicy', 'Bypass', '-File', $runner, '-ProjectPath', $project, '-TestPlatform', 'PlayMode',
                    '-TestFilter', 'Playtest.Bridge.Tests.ReplayPlayModeTests',
                    '-ResultsPath', (Join-Path $output 'playmode-results.xml'), '-LogPath', (Join-Path $output 'playmode.log'))
    if ($UnityPath) { $runnerArgs += @('-UnityPath', $UnityPath) }
    & powershell.exe @runnerArgs
    $provenance.run_exit_code = $LASTEXITCODE
    if ($LASTEXITCODE -ne 0) { throw "The reproduction run failed (exit $LASTEXITCODE); inspect the log and any saved report." }
    if (-not [IO.File]::Exists($report)) { throw 'The reproduction run reported success but wrote no report.' }
    $saved = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
    Write-Host ("Reproduction: {0}  k={1} n={2} unknown={3} attempted={4}" -f $saved.status, $saved.summary.k,
                $saved.summary.n, $saved.summary.unknown, $saved.summary.attempted)
}
finally {
    $provenance.game_after = Get-GitState $project
    [IO.File]::WriteAllText((Join-Path $output 'provenance.json'), ($provenance | ConvertTo-Json -Depth 6) + "`n",
                            (New-Object System.Text.UTF8Encoding($false)))
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $previous[$name]) }
}
