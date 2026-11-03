[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ProjectPath,
    [Parameter(Mandatory = $true)][string]$TracePath,
    [Parameter(Mandatory = $true)][string]$ManifestPath,
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [Parameter(Mandatory = $true)][string]$ControlRef,
    [string]$FindingId,
    [string]$ReproCheckPath,
    [string]$FindingPath,
    [int]$Attempts = 5,
    [double]$SettleSeconds = 3.0,
    [string]$CatalogPath,
    [string]$ScenePath,
    [string]$UnityPath
)

# Reproduce a finding on the game as it is checked out (the seeded build), then on a control build (-ControlRef,
# a branch or commit of the game, such as the unseeded base), with the same plan, and compare the two. A replay
# that reproduces on both shows the game's normal behaviour or a rule, not a defect of the seeded build.
#
# The game's checkout is switched to -ControlRef for the control replay and back to where it started afterwards,
# whatever failed. `git switch` keeps the game's uncommitted wiring files, and refuses a switch they conflict with.
# No model is called. Everything lands in the new output directory:
#   seeded/            reproduce.ps1's output on the build as checked out
#   control/           the same on -ControlRef
#   comparison.json    the verdict: specific, not_specific, not_reproduced or inconclusive
$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$project = [IO.Path]::GetFullPath($ProjectPath).TrimEnd([IO.Path]::DirectorySeparatorChar)
$output = [IO.Path]::GetFullPath($OutputDirectory).TrimEnd([IO.Path]::DirectorySeparatorChar)
if ([IO.Directory]::Exists($output)) { throw 'Choose a new output directory; existing evidence is never overwritten.' }

$branch = (& git -C $project symbolic-ref --short -q HEAD)
if ($LASTEXITCODE -ne 0 -or -not $branch) { $branch = $null }
$commit = (& git -C $project rev-parse HEAD)
if ($LASTEXITCODE -ne 0) { throw 'Could not read the game checkout HEAD.' }
& git -C $project rev-parse --verify --quiet "$ControlRef^{commit}" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "The control ref does not name a commit in the game repository: $ControlRef" }
New-Item -ItemType Directory -Path $output | Out-Null

$common = @{ ProjectPath = $project; TracePath = $TracePath; ManifestPath = $ManifestPath; Attempts = $Attempts;
             SettleSeconds = $SettleSeconds }
foreach ($name in 'FindingId', 'ReproCheckPath', 'FindingPath', 'CatalogPath', 'ScenePath', 'UnityPath') {
    $value = (Get-Variable -Name $name -ValueOnly)
    if ($value) { $common[$name] = $value }
}
$reproduce = Join-Path $PSScriptRoot 'reproduce.ps1'

function Switch-Game([string]$Ref, [bool]$IsBranch) {
    if ($IsBranch) { & git -C $project switch --quiet $Ref } else { & git -C $project switch --quiet --detach $Ref }
    if ($LASTEXITCODE -ne 0) { throw "Could not switch the game checkout to $Ref." }
}

$failure = $null
try {
    & $reproduce @common -OutputDirectory (Join-Path $output 'seeded')
    if ($LASTEXITCODE -ne 0) { throw "The seeded replay failed (exit $LASTEXITCODE)." }
    & git -C $project show-ref --verify --quiet "refs/heads/$ControlRef"
    Switch-Game $ControlRef ($LASTEXITCODE -eq 0)
    & $reproduce @common -OutputDirectory (Join-Path $output 'control')
    if ($LASTEXITCODE -ne 0) { throw "The control replay failed (exit $LASTEXITCODE)." }
}
catch { $failure = $_.Exception.Message }
finally {
    # Back to where the game started, whatever happened.
    if ($branch) { Switch-Game $branch $true } else { Switch-Game $commit $false }
}
if ($failure) { throw $failure }

$python = (& uv run --directory $repoRoot python -c 'import sys; print(sys.executable)')
Push-Location $repoRoot
try {
    & $python -m playtest.runner.reproduce compare --seeded (Join-Path $output 'seeded') `
        --control (Join-Path $output 'control') --output (Join-Path $output 'comparison.json')
    if ($LASTEXITCODE -ne 0) { throw 'Could not compare the seeded and control replays.' }
}
finally { Pop-Location }
