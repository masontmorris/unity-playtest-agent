<#
.SYNOPSIS
Runs S1 in the production turn loop on a wired Unity game, driven by a preflight manifest.

.DESCRIPTION
Hosts the bridge in the Editor-owned live session host (`PlayModeSessionHost`, started by
`play-session-supervisor.ps1` with `-executeMethod`) and starts `playtest.runner.cli` against it
once the host reports its measured initial readiness. The host owns Play Mode, so an S1-selected
restart can stop and start it without killing the client process; the old bench host
(`BenchRecordingPlayModeTests`, used by `run-live-s1.ps1`) stays the host for the bench protocols.
The script first exports the manifest's
catalog with `playtest-export-catalog` (written to `<OutputDirectory>/catalog.json`) and pins its SHA-256.
For this client module the host registers exactly that file, under each declaration's own name, so every
supported chord, stick or mouse value, and skill runs as declared. The host and the client both read the
file through UPA_SESSION_CATALOG and UPA_SESSION_CATALOG_SHA256; if it is absent, altered or invalid, or the
client finds it is not the manifest's export, the run ends before any paid call. A session never falls back
to the keyboard-only catalog the other bench modules use. -S1Arm picks S1's model, one of replay.ARMS
(default liquid-d1-ultra). Its credentials must be in this session's environment: BASETEN_API_KEY for the Baseten
arms, LIQUID_API_KEY and LIQUID_D1.1_URL for the Liquid arms, and MODAL_MAMMOTH_TOKEN_ID,
MODAL_MAMMOTH_TOKEN_SECRET and MODAL_MAMMOTH_URL for mammoth-1-modal (our Flide server on Modal), and
D1_LOCAL_TOKEN and D1_LOCAL_URL for d1-2b-local (the loopback shim in scripts/s1/d1_local_server.py).
The script checks them by name only and never prints them.

No paid call starts without an approval file and a call cap (-MaxCalls). Every call reserves its row in
`<OutputDirectory>/calls` before it is sent. The output directory must be new and outside the Unity
project, and the game must be wired to this checkout (`wire-fpskit.ps1`). The run summary is
`<OutputDirectory>/run.json`; the trace is `trace.jsonl` beside it. Run one session at a time: only one
Editor may own a game project, and the bridge test runner serializes them.

The host's own wall deadline is -WallLimitSeconds plus a 180-second margin, and it accepts at most
1830 seconds, so the wall limit is capped at 1500. The supervisor publishes the out-of-band
`shutdown` marker when the client ends and waits boundedly for the host to leave Play Mode and exit.
Every environment variable the script sets is restored when it exits.

S1's calls are paced to -TokensPerMinute when you give it; without it the runner uses the selected arm's
own default from `playtest.runner.pacing`, so the wrapper never substitutes another arm's rate. The runner
waits before a turn when the last minute's tokens plus the next call's would pass it.

The manifest's readers are installed in the live observation (one Editor field inspection before each
observation) and the run states what they and the actions' effects measured as facts and suspicions; see
docs/design/suspicions.md. -ManifestOrigin is free text recorded in the plan and the summary, for a manifest that
was made on another build and is reused here (say so; a reused manifest is not a fresh preflight).

-Frontier lets the runner call the frontier model when S1 chooses escalate, at most -MaxFrontierCalls times. It needs
META_API_KEY in this session's environment (checked by name only), and the approval file must carry the frontier arm.
Without -Frontier the runner has no frontier helper, exactly as before.

-S1GameView sends a fresh bridge frame to S1 when the selected arm has a verified image transport.
The default d1-ultra is not verified for images; choose a supported arm explicitly.

-RecordFrames records the whole session's play view. A separate process copies every frame the bridge writes into
its rolling capture window, then encodes them after Unity exits, with recording.json beside them. Each play instance
writes into its own generation directory under `Temp/playtest-bridge-frames/<generation>/`, so a restart never mixes
two instances' frames; a video never spans a generation boundary. One generation is encoded as
<OutputDirectory>/recording/session.mp4, and two or more as session-<generation>.mp4 each. It never talks to the
bridge or the runner, and nothing in the session reads what it
writes. The frames are game footage: keep them local.
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
    [double]$MaxAgeSeconds = 0.8,
    [int]$MaxProviderFailures = 3,
    # Only explicitly bound values are used; an explicit zero is invalid.
    [int]$TokensPerMinute = 0,
    [string]$S1Arm = 'liquid-d1-ultra',
    [string]$Objective,
    [string]$ObjectivePath,
    [string]$DiscoveryPath,
    [string]$ScenePath,
    [string]$ManifestOrigin,
    [switch]$Frontier,
    [int]$MaxFrontierCalls = 0,
    [double]$FrontierClipSeconds = 0,
    [switch]$RecordFrames,
    [switch]$S1GameView,
    [switch]$CoverageFacts,
    [switch]$ObjectiveFacts,
    [switch]$PatternFacts,
    [switch]$TravelFacts,
    [switch]$TurnSizes,
    [switch]$PointTurns,
    [switch]$PointMoves,
    [switch]$MeasureGuards,
    [switch]$GuardsOnly,
    [switch]$PressReadings,
    [string]$SituationsPath,
    [ValidateSet('muse-spark-1.3', 'muse-spark-1.3-contributor', 'muse-wonder-1.0')][string]$FrontierModel = 'muse-spark-1.3',
    [string]$ExpectationsPath,
    [ValidateRange(1, 16384)][int]$CaptureWidth = 640,
    [ValidateRange(1, 16384)][int]$CaptureHeight = 480,
    [ValidateSet('full', 'dup-state-ref/1')][string]$S1Layout = 'full',
    [ValidateSet('off', 'object', 'format', 'default', 'all')][string]$S1CompactState = 'off',
    [ValidateSet('follow_up', 'batched')][string]$S1PendingQuestion = 'follow_up',
    [ValidateSet('full', 'compact-menu')][string]$S1Context = 'full',
    [ValidateSet('policy/1', 'direct/1', 'policy/2', 'policy/3')][string]$S1Instructions = 'policy/1',
    [ValidateSet('plain', 'conditional')][string]$FinishLabel = 'plain',
    [switch]$NoFinish,
    [switch]$S1ContinuousInput,
    [switch]$Options,
    [switch]$Placement,
    [switch]$Contingent,
    [ValidateRange(0.0, 1.0)][double]$PacingBurst = 0,
    [ValidateSet('end', 'send')][string]$PacingAnchor = 'end',
    [string]$UnityPath
)

$ErrorActionPreference = 'Stop'
if (($TurnSizes -and $PointTurns) -or ($PointMoves -and ($PointTurns -or $TurnSizes))) { throw '-PointMoves, -PointTurns and -TurnSizes are mutually exclusive.' }
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$project = [IO.Path]::GetFullPath($ProjectPath).TrimEnd([IO.Path]::DirectorySeparatorChar)
$entity = [IO.Path]::GetFullPath($EntityPath)
$calibration = [IO.Path]::GetFullPath($CalibrationPath)
$manifestFile = [IO.Path]::GetFullPath($ManifestPath)
$approval = [IO.Path]::GetFullPath($ApprovalPath)
$output = [IO.Path]::GetFullPath($OutputDirectory).TrimEnd([IO.Path]::DirectorySeparatorChar)
# Only an explicitly given rate is exported; otherwise the runner's per-arm default reaches it untouched.
$tokensPerMinuteGiven = $PSBoundParameters.ContainsKey('TokensPerMinute')
foreach ($required in @($entity, $calibration, $manifestFile, $approval)) {
    if (-not [IO.File]::Exists($required)) { throw "Missing input file: $required" }
}
if ($MaxCalls -lt 1) { throw '-MaxCalls must be at least 1: no paid call starts without a call cap.' }
if ($GuardsOnly -and -not $MeasureGuards) { throw '-GuardsOnly measures the guard table: it needs -MeasureGuards.' }
if ($WallLimitSeconds -lt 30 -or $WallLimitSeconds -gt 1500) { throw '-WallLimitSeconds must be between 30 and 1500.' }
if ($MaxAgeSeconds -le 0) { throw '-MaxAgeSeconds must be positive.' }
if ($MaxProviderFailures -lt 1) { throw '-MaxProviderFailures must be at least 1.' }
if ($tokensPerMinuteGiven -and $TokensPerMinute -lt 1) { throw '-TokensPerMinute must be at least 1.' }
if ($Objective -and $ObjectivePath) { throw 'Give -Objective or -ObjectivePath, not both.' }
if ($ObjectivePath -and -not [IO.File]::Exists([IO.Path]::GetFullPath($ObjectivePath))) { throw "Missing objective file: $ObjectivePath" }
if ($output.Equals($project, [StringComparison]::OrdinalIgnoreCase) -or
    $output.StartsWith($project + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The output directory must be outside the Unity project.'
}
if ([IO.Directory]::Exists($output)) { throw 'Choose a new output directory; existing evidence is never overwritten.' }
# Each S1 arm's credentials, by name only. This mirrors replay.ARMS so a missing key fails before Unity starts;
# the runner checks them again from replay.ARMS itself.
$s1Credentials = @{
    'mammoth-1' = @('BASETEN_API_KEY'); 'elephant-1' = @('BASETEN_API_KEY')
    'liquid-d1.1' = @('LIQUID_API_KEY', 'LIQUID_D1.1_URL'); 'liquid-d1-ultra' = @('LIQUID_API_KEY', 'LIQUID_D1.1_URL')
    'meta-muse-spark-s1' = @('META_API_KEY')
    'mammoth-1-modal' = @('MODAL_MAMMOTH_TOKEN_ID', 'MODAL_MAMMOTH_URL', 'MODAL_MAMMOTH_TOKEN_SECRET')
    'd1-2b-local' = @('D1_LOCAL_TOKEN', 'D1_LOCAL_URL')
}
if (-not $s1Credentials.ContainsKey($S1Arm)) {
    throw "Unknown -S1Arm '$S1Arm'; use one of: $((@($s1Credentials.Keys) | Sort-Object) -join ', ')."
}
foreach ($variable in $s1Credentials[$S1Arm]) {
    if ([string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($variable))) { throw "$variable is not set in this session." }
}
if ($Frontier -and $MaxFrontierCalls -lt 1) { throw '-Frontier needs -MaxFrontierCalls of at least 1: no frontier call starts without a cap.' }
if (-not $Frontier -and $MaxFrontierCalls -ne 0) { throw '-MaxFrontierCalls needs -Frontier.' }
if ($S1Context -eq 'compact-menu' -and -not $Options) {
    # The menu's measured cell comes from the options layer's outcome memory; the runner refuses this too.
    throw '-S1Context compact-menu needs -Options: the menu''s measured cell comes from the options layer.'
}
if ($S1Context -eq 'compact-menu' -and $S1Layout -ne 'full') { throw '-S1Context compact-menu sends its own state; it cannot be combined with -S1Layout.' }
if ($S1Context -eq 'compact-menu' -and $S1CompactState -ne 'off') { throw '-S1Context compact-menu sends its own state; it cannot be combined with -S1CompactState.' }
if ($S1Context -eq 'compact-menu' -and $S1PendingQuestion -ne 'follow_up') { throw '-S1Context compact-menu asks its own pending question beside next_action; it cannot be combined with -S1PendingQuestion.' }
if ($Contingent -and $S1Context -ne 'compact-menu') {
    # The branch answers this acts on are the compact menu's own if_blocked/if_succeeded questions.
    throw '-Contingent needs -S1Context compact-menu: no other mode asks S1 what to do if an option ends blocked.'
}
if ($Frontier -and [string]::IsNullOrEmpty($env:META_API_KEY)) { throw 'META_API_KEY is not set in this session.' }
$packages = Get-Content -LiteralPath (Join-Path $project 'Packages/manifest.json') -Raw | ConvertFrom-Json
$expected = 'file:' + (Join-Path $repoRoot 'bridge').Replace('\', '/')
if (-not [string]::Equals($packages.dependencies.'com.playtest.bridge', $expected, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The game is not wired to this checkout. Run scripts/unity/wire-fpskit.ps1 first.'
}
$python = (& uv run --directory $repoRoot python -c 'import sys; print(sys.executable)')
if ($LASTEXITCODE -ne 0 -or -not [IO.File]::Exists($python)) { throw 'Could not locate the repository Python executable.' }
if ($S1GameView) {
    # The runner's own allowlist decides, so this early check cannot drift from it; asked before Unity starts.
    $imageArms = @(& $python -c 'from playtest.runner.s1 import IMAGE_TRANSPORT; print(chr(10).join(IMAGE_TRANSPORT))')
    if ($LASTEXITCODE -ne 0) { throw 'Could not read the runner''s image allowlist.' }
    if ($S1Arm -notin $imageArms) { throw "-S1GameView: S1 arm '$S1Arm' has no verified image transport." }
}
New-Item -ItemType Directory -Path $output | Out-Null
# What the host serves is the manifest's own export: the exporter validates the manifest and writes only the
# declarations with exact release-verified execution evidence. Its digest is pinned for the host and the client.
$catalogFile = Join-Path $output 'catalog.json'
Push-Location $repoRoot
try {
    $catalogArgs = @()
    if ($TurnSizes) { $catalogArgs += @('--turn-sizes', '--calibration', $calibration) }
    if ($PointMoves) { $catalogArgs += @('--point-moves', '--calibration', $calibration, '--capture-width', $CaptureWidth, '--capture-height', $CaptureHeight) }
    # Each verb chorded with a measured direction, so the runner can measure it while the body moves.
    if ($MeasureGuards) { $catalogArgs += @('--guard-chords', '--calibration', $calibration) }
    if ($PointTurns) { $catalogArgs += @('--point-turns', '--calibration', $calibration, '--capture-width', $CaptureWidth, '--capture-height', $CaptureHeight) }
    & $python -m playtest.preflight.catalog --manifest $manifestFile --output $catalogFile @catalogArgs
}
finally { Pop-Location }
if ($LASTEXITCODE -ne 0 -or -not [IO.File]::Exists($catalogFile)) {
    throw 'Could not export the manifest catalog, so there is nothing for the host to serve. Fix the manifest and choose a new output directory.'
}
$catalogSha = (Get-FileHash -LiteralPath $catalogFile -Algorithm SHA256).Hash.ToLowerInvariant()
$objectiveFile = $null
if ($ObjectivePath) { $objectiveFile = [IO.Path]::GetFullPath($ObjectivePath) }
elseif ($Objective) {
    $objectiveFile = Join-Path $output 'objective.txt'
    [IO.File]::WriteAllText($objectiveFile, $Objective, (New-Object Text.UTF8Encoding($false)))
}
$invariant = [Globalization.CultureInfo]::InvariantCulture
# The Editor owns Play Mode for this run. `play-session-supervisor.ps1` launches it with
# -executeMethod Playtest.Bridge.Editor.PlayModeSessionHost.Start, waits for the host's measured
# initial readiness, and only then starts the client. The mailbox lives outside the game project.
if (-not $ScenePath) {
    $entityDocument = Get-Content -LiteralPath $entity -Raw | ConvertFrom-Json
    $ScenePath = [string]$entityDocument.entity.scene_path
    if (-not $ScenePath) { throw 'The entity record has no selected scene; provide -ScenePath.' }
}
[IO.File]::WriteAllText((Join-Path $output 'starting-scene.json'),
    (@{ scene_path = $ScenePath } | ConvertTo-Json -Compress), (New-Object Text.UTF8Encoding($false)))
$controlDirectory = Join-Path $output 'control'
New-Item -ItemType Directory -Path $controlDirectory | Out-Null
$runId = [guid]::NewGuid().ToString('D')
# Guards are measured before play, inside the host's lifetime: their time is added so the wall limit stays play time.
$guardAllowanceS = if ($MeasureGuards) { 300 } else { 0 }
$hostDeadlineUtc = [DateTime]::UtcNow.AddSeconds($WallLimitSeconds + 180 + $guardAllowanceS)
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
$names = @('UPA_SCENE_PATH', 'UPA_BENCH_OUTPUT', 'UPA_BENCH_PYTHON', 'UPA_BENCH_ENTITY', 'UPA_BENCH_CALIBRATION', 'UPA_BENCH_MODULE',
           'UPA_BENCH_TIMEOUT_S', 'UPA_RUN_MANIFEST', 'UPA_RUN_APPROVAL', 'UPA_RUN_MAX_CALLS', 'UPA_RUN_WALL_LIMIT_S',
           'UPA_RUN_MAX_AGE_S', 'UPA_RUN_MAX_PROVIDER_FAILURES', 'UPA_RUN_TOKENS_PER_MINUTE', 'UPA_RUN_OBJECTIVE_FILE', 'UPA_RUN_DISCOVERY',
           'UPA_RUN_MANIFEST_ORIGIN', 'UPA_SESSION_CATALOG', 'UPA_SESSION_CATALOG_SHA256', 'UPA_RUN_S1_ARM', 'UPA_RUN_FRONTIER',
           'UPA_RUN_MAX_FRONTIER_CALLS', 'UPA_RUN_S1_GAME_VIEW', 'UPA_RUN_FRONTIER_CLIP_S', 'UPA_RUN_S1_LAYOUT', 'UPA_RUN_COVERAGE_FACTS', 'UPA_RUN_PATTERN_FACTS', 'UPA_RUN_TRAVEL_FACTS', 'UPA_RUN_TURN_SIZES', 'UPA_RUN_POINT_TURNS', 'UPA_RUN_POINT_MOVES', 'UPA_RUN_MEASURE_GUARDS', 'UPA_RUN_GUARDS_ONLY', 'UPA_RUN_PRESS_READINGS', 'UPA_RUN_SITUATIONS', 'UPA_RUN_FRONTIER_MODEL', 'UPA_RUN_EXPECTATIONS', 'UPA_RUN_CAPTURE_WIDTH', 'UPA_RUN_CAPTURE_HEIGHT',
           'UPA_RUN_S1_COMPACT_STATE', 'UPA_RUN_S1_PENDING_QUESTION', 'UPA_RUN_S1_INSTRUCTIONS', 'UPA_RUN_S1_CONTEXT',
           'UPA_RUN_FINISH_LABEL', 'UPA_RUN_NO_FINISH', 'UPA_RUN_PACING_BURST', 'UPA_RUN_PACING_ANCHOR', 'UPA_RUN_CONTINUOUS_INPUT',
           'UPA_RUN_OPTIONS', 'UPA_RUN_CONTINGENT', 'UPA_RUN_PLACEMENT', 'UPA_CONTROLLED_ENTITY')
$previous = @{}
foreach ($name in $names) { $previous[$name] = [Environment]::GetEnvironmentVariable($name) }
$recorder = $null
try {
    $env:UPA_SCENE_PATH = $ScenePath
    $env:UPA_BENCH_OUTPUT = Join-Path $output 'run.json'
    $env:UPA_BENCH_PYTHON = $python
    $env:UPA_BENCH_ENTITY = $entity
    $env:UPA_BENCH_CALIBRATION = $calibration
    $env:UPA_BENCH_MODULE = 'playtest.runner.cli'
    $env:UPA_BENCH_TIMEOUT_S = [string]($WallLimitSeconds + 180)
    $env:UPA_SESSION_CATALOG = $catalogFile
    $env:UPA_SESSION_CATALOG_SHA256 = $catalogSha
    $env:UPA_RUN_MANIFEST = $manifestFile
    $env:UPA_RUN_APPROVAL = $approval
    $env:UPA_RUN_MAX_CALLS = [string]$MaxCalls
    $env:UPA_RUN_WALL_LIMIT_S = [string]$WallLimitSeconds
    $env:UPA_RUN_MAX_AGE_S = $MaxAgeSeconds.ToString($invariant)
    $env:UPA_RUN_MAX_PROVIDER_FAILURES = [string]$MaxProviderFailures
    if ($tokensPerMinuteGiven) { $env:UPA_RUN_TOKENS_PER_MINUTE = [string]$TokensPerMinute }
    $env:UPA_RUN_DISCOVERY = if ($DiscoveryPath) { [IO.Path]::GetFullPath($DiscoveryPath) } else { '' }
    $env:UPA_RUN_OBJECTIVE_FILE = if ($objectiveFile) { $objectiveFile } else { '' }
    $env:UPA_RUN_MANIFEST_ORIGIN = if ($ManifestOrigin) { $ManifestOrigin } else { '' }
    $env:UPA_RUN_S1_ARM = $S1Arm
    $env:UPA_RUN_S1_GAME_VIEW = if ($S1GameView) { '1' } else { '' }
    $env:UPA_RUN_S1_LAYOUT = $S1Layout
    $env:UPA_RUN_S1_COMPACT_STATE = $S1CompactState
    $env:UPA_RUN_S1_PENDING_QUESTION = $S1PendingQuestion
    $env:UPA_RUN_S1_CONTEXT = $S1Context
    $env:UPA_RUN_S1_INSTRUCTIONS = $S1Instructions
    $env:UPA_RUN_FINISH_LABEL = $FinishLabel
    $env:UPA_RUN_NO_FINISH = if ($NoFinish) { '1' } else { '' }
    $env:UPA_RUN_CONTINUOUS_INPUT = if ($S1ContinuousInput) { '1' } else { '' }
    $env:UPA_RUN_OPTIONS = if ($Options) { '1' } else { '' }
    $env:UPA_RUN_CONTINGENT = if ($Contingent) { '1' } else { '' }
    $env:UPA_RUN_PLACEMENT = if ($Placement) { '1' } else { '' }
    $controlledEntityDocument = Get-Content -LiteralPath $entity -Raw | ConvertFrom-Json
    $env:UPA_CONTROLLED_ENTITY = [string]$controlledEntityDocument.entity.path
    $env:UPA_RUN_PACING_BURST = $PacingBurst.ToString($invariant)
    $env:UPA_RUN_PACING_ANCHOR = $PacingAnchor
    $env:UPA_RUN_POINT_MOVES = if ($PointMoves) { '1' } else { '' }
    $env:UPA_RUN_MEASURE_GUARDS = if ($MeasureGuards) { '1' } else { '' }
    $env:UPA_RUN_GUARDS_ONLY = if ($GuardsOnly) { '1' } else { '' }
    $env:UPA_RUN_PRESS_READINGS = if ($PressReadings) { '1' } else { '' }
    $env:UPA_RUN_SITUATIONS = if ($SituationsPath) { [IO.Path]::GetFullPath($SituationsPath) } else { '' }
    $env:UPA_RUN_FRONTIER_MODEL = $FrontierModel
    $env:UPA_RUN_EXPECTATIONS = if ($ExpectationsPath) { [IO.Path]::GetFullPath($ExpectationsPath) } else { '' }
    $env:UPA_RUN_POINT_TURNS = if ($PointTurns) { '1' } else { '' }
    $env:UPA_RUN_CAPTURE_WIDTH = [string]$CaptureWidth
    $env:UPA_RUN_CAPTURE_HEIGHT = [string]$CaptureHeight
    $env:UPA_RUN_TURN_SIZES = if ($TurnSizes) { '1' } else { '' }
    $env:UPA_RUN_PATTERN_FACTS = if ($PatternFacts) { '1' } else { '' }
    $env:UPA_RUN_TRAVEL_FACTS = if ($TravelFacts) { '1' } else { '' }
    $env:UPA_RUN_COVERAGE_FACTS = if ($CoverageFacts) { '1' } else { '' }
    $env:UPA_RUN_OBJECTIVE_FACTS = if ($ObjectiveFacts) { '1' } else { '' }
    $env:UPA_RUN_FRONTIER = if ($Frontier) { '1' } else { '' }
    $env:UPA_RUN_MAX_FRONTIER_CALLS = if ($Frontier) { [string]$MaxFrontierCalls } else { '' }
    $env:UPA_RUN_FRONTIER_CLIP_S = if ($FrontierClipSeconds -gt 0) { [string]$FrontierClipSeconds } else { '' }
    $runner = Join-Path $PSScriptRoot 'play-session-supervisor.ps1'
    $runnerArgs = @('-ExecutionPolicy', 'Bypass', '-File', $runner,
                    '-ProjectPath', $project, '-ControlDirectory', $controlDirectory,
                    '-PythonPath', $python, '-ClientModule', 'playtest.runner.cli',
                    '-ClientProject', $project, '-ClientEntity', $entity, '-ClientCalibration', $calibration,
                    '-ClientOutput', $env:UPA_BENCH_OUTPUT,
                    '-InitialReadyTimeoutSeconds', '300', '-ShutdownTimeoutSeconds', '180')
    if ($UnityPath) { $runnerArgs += @('-UnityPath', $UnityPath) }
    if ($RecordFrames) {
        # Started before Unity so the first generation is caught; it follows the host's published ready
        # generations and reads each play instance's own frame directory, never the legacy flat window.
        $recordDir = Join-Path $output 'recording'
        $recordStop = Join-Path $output 'recording.stop'
        $recordArgs = '-m playtest.runner.session_video --project "' + $project + '" --output "' + $recordDir +
                      '" --stop-file "' + $recordStop + '" --control-directory "' + $controlDirectory +
                      '" --max-wall-s ' + [string]($WallLimitSeconds + 900)
        $recorder = Start-Process -FilePath $python -ArgumentList $recordArgs -WorkingDirectory $repoRoot -NoNewWindow `
            -PassThru -RedirectStandardOutput (Join-Path $output 'recording.out.log') `
            -RedirectStandardError (Join-Path $output 'recording.err.log')
    }
    & powershell.exe @runnerArgs
    $sessionExit = $LASTEXITCODE
    if ($sessionExit -ne 0) { throw "The session failed (exit $sessionExit); inspect the retained summary, trace and log." }
    Write-Host "Served catalog: $catalogFile (sha256 $catalogSha)"
    Write-Host "Saved run summary: $env:UPA_BENCH_OUTPUT"
    Write-Host "Saved trace: $(Join-Path $output 'trace.jsonl')"
}
finally {
    if ($recorder) {
        # Unity has exited, so no frame is still to come: stop the recorder and let it encode.
        New-Item -ItemType File -Path $recordStop -Force | Out-Null
        if (-not $recorder.WaitForExit(900000)) {
            Write-Warning 'The session recorder did not finish encoding within 15 minutes; stopping it.'
            Stop-Process -Id $recorder.Id -Force -ErrorAction SilentlyContinue
        }
        $recordSummary = Join-Path $recordDir 'recording.json'
        if ([IO.File]::Exists($recordSummary)) { Write-Host "Saved session recording: $recordSummary" }
        else { Write-Warning "The session recorder wrote no summary; see $(Join-Path $output 'recording.err.log')." }
    }
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $previous[$name]) }
}
