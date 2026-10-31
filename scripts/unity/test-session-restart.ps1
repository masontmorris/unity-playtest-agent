<#
.SYNOPSIS
Runs the free restart validation: one Editor-owned Play Mode session, no model and no paid call.

.DESCRIPTION
A scripted smoke test for the restart path. It exports the manifest's catalog exactly as `run-session.ps1`
does, pins its SHA-256, writes the session's `config.json`, and hands the run to
`play-session-supervisor.ps1`, which starts Unity with
`-executeMethod Playtest.Bridge.Editor.PlayModeSessionHost.Start` and then starts
`playtest.runner.restart_smoke`. That client selects each restart itself through the mailbox; nothing here
invents a request, retries an operation or reads a control record.

There is no approval file and no call cap because this run makes no model call. The output directory must be
new and outside the game project; the catalog, the control records and the client's summary stay there. Only
one Editor may own the game project, so run it alone.

Exit codes:
  0  the client and the host both finished cleanly
  1  the client failed, or the host failed, timed out or had to be killed
  2  setup failed and Unity was not started
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ProjectPath,
    [Parameter(Mandatory = $true)][string]$EntityPath,
    [Parameter(Mandatory = $true)][string]$CalibrationPath,
    [Parameter(Mandatory = $true)][string]$ManifestPath,
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [int]$WallLimitSeconds = 300,
    [string]$UnityPath
)

$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$project = [IO.Path]::GetFullPath($ProjectPath).TrimEnd([IO.Path]::DirectorySeparatorChar)
$entity = [IO.Path]::GetFullPath($EntityPath)
$calibration = [IO.Path]::GetFullPath($CalibrationPath)
$manifestFile = [IO.Path]::GetFullPath($ManifestPath)
$output = [IO.Path]::GetFullPath($OutputDirectory).TrimEnd([IO.Path]::DirectorySeparatorChar)
foreach ($required in @($entity, $calibration, $manifestFile)) {
    if (-not [IO.File]::Exists($required)) { throw "Missing input file: $required" }
}
if ($WallLimitSeconds -lt 30) { throw '-WallLimitSeconds must be at least 30.' }
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
$python = (& uv run --directory $repoRoot python -c 'import sys; print(sys.executable)')
if ($LASTEXITCODE -ne 0 -or -not [IO.File]::Exists($python)) { throw 'Could not locate the repository Python executable.' }
New-Item -ItemType Directory -Path $output | Out-Null
$catalogFile = Join-Path $output 'catalog.json'
Push-Location $repoRoot
try { & $python -m playtest.preflight.catalog --manifest $manifestFile --output $catalogFile }
finally { Pop-Location }
if ($LASTEXITCODE -ne 0 -or -not [IO.File]::Exists($catalogFile)) {
    throw 'Could not export the manifest catalog, so the host would have nothing to serve.'
}
$catalogSha = (Get-FileHash -LiteralPath $catalogFile -Algorithm SHA256).Hash.ToLowerInvariant()

$invariant = [Globalization.CultureInfo]::InvariantCulture
$controlDirectory = Join-Path $output 'control'
New-Item -ItemType Directory -Path $controlDirectory | Out-Null
$runId = [guid]::NewGuid().ToString('D')
$hostDeadlineUtc = [DateTime]::UtcNow.AddSeconds($WallLimitSeconds + 180)
$sessionConfig = [ordered]@{
    version        = 1
    run_id         = $runId
    project_path   = $project
    catalog_path   = $catalogFile
    catalog_sha256 = $catalogSha
    deadline_utc   = $hostDeadlineUtc.ToString("yyyy-MM-dd'T'HH:mm:ss.fffffff'Z'", $invariant)
}
[IO.File]::WriteAllText((Join-Path $controlDirectory 'config.json'), ($sessionConfig | ConvertTo-Json -Compress),
    (New-Object Text.UTF8Encoding($false)))

$names = @('UPA_SESSION_CATALOG', 'UPA_SESSION_CATALOG_SHA256')
$previous = @{}
foreach ($name in $names) { $previous[$name] = [Environment]::GetEnvironmentVariable($name) }
try {
    $env:UPA_SESSION_CATALOG = $catalogFile
    $env:UPA_SESSION_CATALOG_SHA256 = $catalogSha
    $runner = Join-Path $PSScriptRoot 'play-session-supervisor.ps1'
    $runnerArgs = @('-ExecutionPolicy', 'Bypass', '-File', $runner,
                    '-ProjectPath', $project, '-ControlDirectory', $controlDirectory,
                    '-PythonPath', $python, '-ClientModule', 'playtest.runner.restart_smoke',
                    '-ClientProject', $project, '-ClientEntity', $entity, '-ClientCalibration', $calibration,
                    '-ClientManifest', $manifestFile, '-ClientOutput', (Join-Path $output 'restart.json'),
                    '-InitialReadyTimeoutSeconds', '300', '-ShutdownTimeoutSeconds', '180')
    if ($UnityPath) { $runnerArgs += @('-UnityPath', $UnityPath) }
    & powershell.exe @runnerArgs
    $sessionExit = $LASTEXITCODE
    if ($sessionExit -ne 0) {
        throw "The restart validation failed (exit $sessionExit); inspect the control records, the Unity log and the summary."
    }
    Write-Host "Served catalog: $catalogFile (sha256 $catalogSha)"
    Write-Host "Saved restart summary: $(Join-Path $output 'restart.json')"
    Write-Host "Control records: $controlDirectory"
}
finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $previous[$name]) }
}
