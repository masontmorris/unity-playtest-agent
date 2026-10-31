<#
.SYNOPSIS
Supervises one Editor-owned Play Mode session: Unity hosts Play Mode, Python is the client.

.DESCRIPTION
The caller has already written `config.json` into a new control directory and set the catalog the
host must serve. This script starts Unity in batch mode with
`-executeMethod Playtest.Bridge.Editor.PlayModeSessionHost.Start` (deliberately without `-runTests`
or `-quit`, either of which would end the host before the session does), waits for the host's
initial `ready` in `host-state.json`, and only then starts the Python client. It owns both process
objects: while the client runs it watches the host and the validated absolute `deadline_utc`, and on
a deadline, a host failure or either process ending it stops only the client it started. When the
client ends it publishes the out-of-band `shutdown` marker and waits boundedly for the host to leave
Play Mode and exit, stopping only that host process if it does not.

It never selects a restart, never retries, and never edits a control record. `UPA_SESSION_CONTROL_DIRECTORY`
is set here for both children; the config, the ticket ids and the generations are the mailbox's. It takes
the same machine-wide mutex as the bridge test runner, so a session and a test run never own one project at
the same time.

The client's stdout and stderr are kept as bounded, run-local evidence beside the control records
(`client.stdout.log`, `client.stderr.log`); a failing run prints only a bounded tail of each. Polling
here is local process supervision, never an S1 turn.

Exit codes:
  0  the client and the host both finished cleanly
  1  the client failed, or the host failed, timed out or had to be killed
  2  setup failed and Unity was not started
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ProjectPath,
    [Parameter(Mandatory = $true)][string]$ControlDirectory,
    [Parameter(Mandatory = $true)][string]$PythonPath,
    [Parameter(Mandatory = $true)][string]$ClientModule,
    [Parameter(Mandatory = $true)][string]$ClientProject,
    [string]$ClientEntity,
    [string]$ClientCalibration,
    [string]$ClientManifest,
    [string]$ClientOutput,
    [string]$UnityPath,
    [string]$LogPath,
    [int]$InitialReadyTimeoutSeconds = 300,
    [int]$ShutdownTimeoutSeconds = 120,
    [int]$MutexTimeoutMinutes = 15,
    [switch]$NoGraphics
)

$ErrorActionPreference = 'Stop'
$invariant = [Globalization.CultureInfo]::InvariantCulture
$DefaultUnityPath = 'C:\Program Files\Unity\Hub\Editor\6000.5.4f1\Editor\Unity.exe'
$EntryPoint = 'Playtest.Bridge.Editor.PlayModeSessionHost.Start'
$ControlVariable = 'UPA_SESSION_CONTROL_DIRECTORY'
$HostStateName = 'host-state.json'
$ShutdownName = 'shutdown'
$ClientStdoutName = 'client.stdout.log'
$ClientStderrName = 'client.stderr.log'
$PollMilliseconds = 250
$ExitClean = 0
$ExitFailed = 1
$ExitSetup = 2
# The same machine-wide mutex the bridge test runner takes: a supervised session and a test run
# must never own one project at the same time.
$MutexNames = @(
    'Global\unity-playtest-agent-editmode-tests',
    'Local\unity-playtest-agent-editmode-tests'
)

$setupFailure = $false
$unityProcess = $null
$clientProcess = $null
$clientHandle = $null
$unityStarted = $false
$unityKilled = $false
$clientStopped = $false
$exitCode = $ExitSetup
$mutex = $null
$mutexHeld = $false
$clientStdoutPath = $null
$clientStderrPath = $null

function Stop-Setup {
    param([string]$Message)
    $script:setupFailure = $true
    throw $Message
}

function New-BridgeTestMutex {
    foreach ($name in $MutexNames) {
        try {
            $created = [System.Threading.Mutex]::new($false, $name)
            return [pscustomobject]@{ Mutex = $created; Name = $name }
        }
        catch [System.UnauthorizedAccessException] {
            Write-Verbose "Could not create mutex '$name': access denied."
        }
        catch [System.IO.IOException] {
            Write-Verbose "Could not create mutex '$name': $($_.Exception.Message)"
        }
    }
    Stop-Setup 'Could not create either bridge test mutex (Global or Local).'
}

# Quote one argument for the Windows command line (the CommandLineToArgvW / MSVCRT rules), so a
# path with spaces, a quote or a trailing backslash reaches the child unchanged. Start-Process
# joins an -ArgumentList array with spaces without quoting, so the whole line is built here.
function ConvertTo-QuotedArgument {
    param([AllowEmptyString()][string]$Value)
    if ($null -eq $Value -or $Value.Length -eq 0) { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }

    $builder = [Text.StringBuilder]::new($Value.Length + 2)
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes++
            continue
        }
        if ($character -eq '"') {
            [void]$builder.Append([char]'\', ($backslashes * 2) + 1)
            [void]$builder.Append('"')
        }
        else {
            if ($backslashes -gt 0) { [void]$builder.Append([char]'\', $backslashes) }
            [void]$builder.Append($character)
        }
        $backslashes = 0
    }
    if ($backslashes -gt 0) { [void]$builder.Append([char]'\', $backslashes * 2) }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function ConvertTo-ArgumentLine {
    param([string[]]$Arguments)
    return (($Arguments | ForEach-Object { ConvertTo-QuotedArgument ([string]$_) }) -join ' ')
}

# An absolute instant must name its offset. A missing or naive value is invalid, never local time.
function ConvertTo-UtcInstant {
    param([object]$Value)
    if ($null -eq $Value) { return $null }
    $text = [string]$Value
    if ([string]::IsNullOrWhiteSpace($text)) { return $null }
    if ($text -notmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,7})?(Z|[+-]\d{2}:\d{2})$') {
        return $null
    }
    try {
        $parsed = [DateTimeOffset]::Parse($text, $invariant, [Globalization.DateTimeStyles]::None)
        return $parsed.UtcDateTime
    }
    catch {
        return $null
    }
}

function Write-FileTail {
    param([string]$Path, [string]$Label, [int]$LineCount = 40, [int]$MaxCharacters = 4000)
    if ([string]::IsNullOrWhiteSpace($Path) -or -not [IO.File]::Exists($Path)) { return }
    try {
        $text = (Get-Content -LiteralPath $Path -Tail $LineCount -ErrorAction Stop) -join [Environment]::NewLine
        if ($text.Length -gt $MaxCharacters) { $text = $text.Substring($text.Length - $MaxCharacters) }
        if ([string]::IsNullOrWhiteSpace($text)) { return }
        Write-Host "--- last $LineCount $Label line(s): $Path ---"
        Write-Host $text
        Write-Host "--- end of $Label ---"
    }
    catch {
        Write-Host "($Label could not be read: $($_.Exception.Message))"
    }
}

function Write-LogTail {
    param([string]$Path, [int]$LineCount = 40)
    if ([string]::IsNullOrWhiteSpace($Path) -or -not [IO.File]::Exists($Path)) {
        Write-Host "Unity log: (not written) $Path"
        return
    }
    Write-FileTail -Path $Path -Label 'Unity log' -LineCount $LineCount
}

function Read-HostState {
    param([string]$Path)
    try {
        if (-not [IO.File]::Exists($Path)) { return $null }
        $text = [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8)
        if ([string]::IsNullOrWhiteSpace($text)) { return $null }
        return $text | ConvertFrom-Json
    }
    catch [IO.IOException] {
        return $null
    }
    catch [UnauthorizedAccessException] {
        return $null
    }
}

# Stops only the process object this script started, by its own id. It never searches for or stops
# another python.exe, Unity.exe or unrelated process on the machine.
function Stop-OwnedProcess {
    param($Process, [string]$Label)
    if ($null -eq $Process) { return $false }
    try {
        if ($Process.HasExited) { return $false }
        Write-Host "Stopping the $Label process this run started (pid $($Process.Id))."
        Stop-Process -Id $Process.Id -Force -ErrorAction Stop
        [void]$Process.WaitForExit(30000)
        return $true
    }
    catch {
        Write-Warning "Could not stop the $Label process (pid $($Process.Id)): $($_.Exception.Message)"
        return $false
    }
}

# Starts the client as a real background process with hidden window and redirected streams. Windows
# PowerShell 5.1's Start-Process loses the exit code when both streams are redirected, so the client
# is started through System.Diagnostics.Process directly; Unity (no redirection) still uses
# Start-Process. The streams are copied asynchronously so a chatty client can never block on a full
# pipe, and the supervisor keeps the Process object for its own pid only.
function Start-OwnedClient {
    param([string]$FilePath, [string]$Arguments, [string]$StdoutPath, [string]$StderrPath)

    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = $Arguments
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true

    $process = New-Object Diagnostics.Process
    $process.StartInfo = $startInfo
    $stdout = $null
    $stderr = $null
    try {
        $stdout = [IO.File]::Create($StdoutPath)
        $stderr = [IO.File]::Create($StderrPath)
        if (-not $process.Start()) {
            throw 'the client process did not start'
        }
        return [pscustomobject]@{
            Process = $process
            Stdout  = $stdout
            Stderr  = $stderr
            OutTask = $process.StandardOutput.BaseStream.CopyToAsync($stdout)
            ErrTask = $process.StandardError.BaseStream.CopyToAsync($stderr)
        }
    }
    catch {
        try { if (-not $process.HasExited) { $process.Kill() } } catch { }
        if ($null -ne $stdout) { try { $stdout.Dispose() } catch { } }
        if ($null -ne $stderr) { try { $stderr.Dispose() } catch { } }
        try { $process.Dispose() } catch { }
        throw
    }
}

# Waits for the redirected streams to drain and closes the evidence files. It never throws: a closed
# stream or a late copy task must not change the run's outcome.
function Complete-OwnedClient {
    param($Client)
    if ($null -eq $Client) { return }
    try { if (-not $Client.Process.HasExited) { return } } catch { return }
    try { $Client.Process.WaitForExit() } catch { }
    try { [void]$Client.OutTask.Wait(10000) } catch { }
    try { [void]$Client.ErrTask.Wait(10000) } catch { }
    foreach ($stream in @($Client.Stdout, $Client.Stderr)) {
        if ($null -eq $stream) { continue }
        try { $stream.Flush() } catch { }
        try { $stream.Dispose() } catch { }
    }
}

function Wait-InitialReady {
    param([string]$Path, [string]$RunId, [DateTime]$DeadlineUtc, [int]$TimeoutSeconds, $UnityProcess)

    $windowEnd = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $readyBy = if ($windowEnd -lt $DeadlineUtc) { $windowEnd } else { $DeadlineUtc }

    while ([DateTime]::UtcNow -lt $readyBy) {
        if ($null -ne $UnityProcess -and $UnityProcess.HasExited) {
            throw "Unity exited (code $($UnityProcess.ExitCode)) before the host reported its initial readiness."
        }

        $state = Read-HostState $Path
        if ($null -ne $state) {
            if ([string]$state.run_id -ne $RunId) {
                throw "host-state.json belongs to run '$($state.run_id)', not '$RunId'; this control directory is not this run's."
            }

            switch ([string]$state.phase) {
                'ready' {
                    if ([string]::IsNullOrWhiteSpace([string]$state.generation) -or
                        [string]::IsNullOrWhiteSpace([string]$state.scene_path) -or
                        $null -eq $state.scene_handle -or
                        [string]::IsNullOrWhiteSpace([string]$state.ready_utc)) {
                        throw 'The host reported ready without a measured generation, scene and ready time.'
                    }
                    if ($null -eq (ConvertTo-UtcInstant $state.ready_utc)) {
                        throw "The host reported a ready time without a timezone offset: '$($state.ready_utc)'."
                    }
                    return $state
                }
                'failed' {
                    throw "The host failed before its initial readiness: $($state.error)"
                }
            }
        }

        $remaining = ($readyBy - [DateTime]::UtcNow).TotalMilliseconds
        if ($remaining -le 0) { break }
        Start-Sleep -Milliseconds ([int][Math]::Max(1, [Math]::Min($PollMilliseconds, $remaining)))
    }

    if ($readyBy -eq $DeadlineUtc) {
        throw "The session deadline $($DeadlineUtc.ToString('o', $invariant)) passed before the host reported its initial readiness."
    }
    throw "The host did not report its initial readiness within $TimeoutSeconds second(s)."
}

try {
    if (-not (Test-Path -LiteralPath $ProjectPath -PathType Container)) {
        Stop-Setup "Unity project not found: $ProjectPath"
    }
    $projectFull = (Resolve-Path -LiteralPath $ProjectPath).ProviderPath
    $tempDirectory = Join-Path $projectFull 'Temp'
    $editorLock = Join-Path $tempDirectory 'UnityLockfile'
    if ([IO.File]::Exists($editorLock)) {
        Stop-Setup "The project looks open in a Unity Editor ('$editorLock' exists). Close that Editor, or delete the stale lock file, then retry."
    }
    if ($MutexTimeoutMinutes -lt 0) { Stop-Setup 'MutexTimeoutMinutes cannot be negative.' }

    # Acquire the bridge-test mutex before anything else launches, then re-check the project lock
    # while holding it: a test run that started in between is the only remaining owner.
    $mutexHandle = New-BridgeTestMutex
    $mutex = $mutexHandle.Mutex
    Write-Host "Mutex:       $($mutexHandle.Name) (waiting up to $MutexTimeoutMinutes minute(s))"
    $acquired = $false
    try {
        $acquired = $mutex.WaitOne([TimeSpan]::FromMinutes([double]$MutexTimeoutMinutes))
    }
    catch [System.Threading.AbandonedMutexException] {
        $acquired = $true
        Write-Warning 'The previous run abandoned the mutex; continuing as the owner.'
    }
    if (-not $acquired) {
        Stop-Setup "Timed out after $MutexTimeoutMinutes minute(s) waiting for another run on this machine (mutex '$($mutexHandle.Name)')."
    }
    $mutexHeld = $true
    if ([IO.File]::Exists($editorLock)) {
        Stop-Setup "The project looks open in a Unity Editor ('$editorLock' exists). Close that Editor, or delete the stale lock file, then retry."
    }

    if (-not (Test-Path -LiteralPath $ControlDirectory -PathType Container)) {
        Stop-Setup "The session control directory does not exist: $ControlDirectory"
    }
    $control = (Resolve-Path -LiteralPath $ControlDirectory).ProviderPath
    $configPath = Join-Path $control 'config.json'
    if (-not [IO.File]::Exists($configPath)) {
        Stop-Setup "config.json is missing from the session control directory: $configPath"
    }
    $hostStatePath = Join-Path $control $HostStateName
    if ([IO.File]::Exists($hostStatePath)) {
        Stop-Setup "The control directory already holds a host-state.json; choose a new directory for a new run."
    }

    $config = [IO.File]::ReadAllText($configPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    if ([string]::IsNullOrWhiteSpace([string]$config.run_id)) {
        Stop-Setup "config.json has no run_id: $configPath"
    }
    $runId = [string]$config.run_id
    if (-not [string]::Equals([string]$config.project_path, $projectFull, [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Setup "config.json project_path '$($config.project_path)' is not the project being launched '$projectFull'."
    }

    # The deadline bounds readiness and the client's whole lifetime. It must be an absolute,
    # timezone-aware instant; a missing, naive or malformed value is a setup error, never a default.
    $deadlineUtc = ConvertTo-UtcInstant $config.deadline_utc
    if ($null -eq $deadlineUtc) {
        Stop-Setup ("config.json deadline_utc must be an ISO 8601 instant with Z or an explicit offset " +
            "(for example 2026-01-02T03:04:05Z); it is missing or invalid: '$($config.deadline_utc)'")
    }
    if ($deadlineUtc -le [DateTime]::UtcNow) {
        Stop-Setup "config.json deadline_utc '$($config.deadline_utc)' has already passed."
    }

    if ([string]::IsNullOrWhiteSpace($PythonPath) -or -not [IO.File]::Exists($PythonPath)) {
        Stop-Setup "The Python executable is missing: $PythonPath"
    }
    if ([string]::IsNullOrWhiteSpace($ClientModule)) {
        Stop-Setup 'The client module is missing, so there is nothing to run.'
    }
    if ([string]::IsNullOrWhiteSpace($ClientProject) -or -not [IO.Directory]::Exists($ClientProject)) {
        Stop-Setup "The client's project does not exist: $ClientProject"
    }
    foreach ($argumentPath in @($ClientEntity, $ClientCalibration, $ClientManifest)) {
        if (-not [string]::IsNullOrWhiteSpace($argumentPath) -and -not [IO.File]::Exists($argumentPath)) {
            Stop-Setup "The client input file does not exist: $argumentPath"
        }
    }
    if ($InitialReadyTimeoutSeconds -lt 1) { Stop-Setup 'InitialReadyTimeoutSeconds must be at least 1.' }
    if ($ShutdownTimeoutSeconds -lt 1) { Stop-Setup 'ShutdownTimeoutSeconds must be at least 1.' }

    if ([string]::IsNullOrWhiteSpace($UnityPath)) {
        $fromEnvironment = [Environment]::GetEnvironmentVariable('UNITY_EDITOR')
        if ([string]::IsNullOrWhiteSpace($fromEnvironment)) { $UnityPath = $DefaultUnityPath }
        else { $UnityPath = $fromEnvironment }
    }
    if (-not (Test-Path -LiteralPath $UnityPath -PathType Leaf)) {
        Stop-Setup "Unity editor not found: $UnityPath (pass -UnityPath or set UNITY_EDITOR)"
    }
    $unityFull = (Resolve-Path -LiteralPath $UnityPath).ProviderPath

    if ([string]::IsNullOrWhiteSpace($LogPath)) { $LogPath = Join-Path $control 'unity.log' }
    $LogPath = [IO.Path]::GetFullPath($LogPath)
    $logDirectory = [IO.Path]::GetDirectoryName($LogPath)
    if (-not [IO.Directory]::Exists($logDirectory)) {
        try { [void][IO.Directory]::CreateDirectory($logDirectory) }
        catch { Stop-Setup "Cannot create the log directory '$logDirectory': $($_.Exception.Message)" }
    }
    $clientStdoutPath = Join-Path $control $ClientStdoutName
    $clientStderrPath = Join-Path $control $ClientStderrName

    # The host and the client both read this, and the host refuses to auto-start an unrelated bridge.
    [Environment]::SetEnvironmentVariable($ControlVariable, $control)

    Write-Host "Project:     $projectFull"
    Write-Host "Unity:       $unityFull"
    Write-Host "Control:     $control"
    Write-Host "Run:         $runId"
    Write-Host "Deadline:    $($deadlineUtc.ToString('o', $invariant))"
    Write-Host "Log:         $LogPath"

    $unityArguments = @('-batchmode', '-projectPath', $projectFull, '-executeMethod', $EntryPoint, '-logFile', $LogPath)
    if ($NoGraphics) { $unityArguments = @('-batchmode', '-nographics', '-projectPath', $projectFull, '-executeMethod', $EntryPoint, '-logFile', $LogPath) }
    $unityArgumentLine = ConvertTo-ArgumentLine $unityArguments
    Write-Host "Running:     `"$unityFull`" $unityArgumentLine"

    $unityProcess = Start-Process -FilePath $unityFull -ArgumentList $unityArgumentLine -WindowStyle Hidden -PassThru
    $unityStarted = $true
    $exitCode = $ExitFailed

    $state = Wait-InitialReady -Path $hostStatePath -RunId $runId -DeadlineUtc $deadlineUtc `
        -TimeoutSeconds $InitialReadyTimeoutSeconds -UnityProcess $unityProcess
    Write-Host "Initial ready: generation $($state.generation), scene $($state.scene_path) (handle $($state.scene_handle)) at $($state.ready_utc)"

    # The client gets its paths as arguments and everything else from the environment the caller set.
    $clientArguments = @('-m', $ClientModule, '--project', $ClientProject)
    if (-not [string]::IsNullOrWhiteSpace($ClientEntity)) { $clientArguments += @('--entity', $ClientEntity) }
    if (-not [string]::IsNullOrWhiteSpace($ClientCalibration)) { $clientArguments += @('--calibration', $ClientCalibration) }
    if (-not [string]::IsNullOrWhiteSpace($ClientManifest)) { $clientArguments += @('--manifest', $ClientManifest) }
    if (-not [string]::IsNullOrWhiteSpace($ClientOutput)) { $clientArguments += @('--output', $ClientOutput) }
    $clientArgumentLine = ConvertTo-ArgumentLine $clientArguments
    Write-Host "Client:      $PythonPath $clientArgumentLine"
    Write-Host "Client logs: $clientStdoutPath ; $clientStderrPath"

    # Start the client in the background so this script can keep owning both processes. The hidden
    # window and full stdout/stderr redirection keep the run's evidence local and bounded; every
    # argument is already quoted by the same rule for the Unity line.
    $clientHandle = Start-OwnedClient -FilePath $PythonPath -Arguments $clientArgumentLine `
        -StdoutPath $clientStdoutPath -StderrPath $clientStderrPath
    $clientProcess = $clientHandle.Process
    Write-Host "Client pid:  $($clientProcess.Id)"

    # Local operational supervision while the client runs. This is not an S1 turn: it only watches
    # the two processes and the absolute deadline, and it never selects a restart or retries.
    $clientFailure = $null
    while (-not $clientProcess.HasExited) {
        if ($unityProcess.HasExited) {
            $clientFailure = "The session host exited (code $($unityProcess.ExitCode)) while the client was still running; the host must outlive its client."
            break
        }

        $state = Read-HostState $hostStatePath
        if ($null -ne $state -and [string]$state.run_id -eq $runId -and [string]$state.phase -eq 'failed') {
            $clientFailure = "The session host reported failure while the client was running: $($state.error)"
            break
        }

        $now = [DateTime]::UtcNow
        if ($now -ge $deadlineUtc) {
            $clientFailure = "The session deadline $($deadlineUtc.ToString('o', $invariant)) passed while the client was still running."
            break
        }

        $remaining = ($deadlineUtc - $now).TotalMilliseconds
        Start-Sleep -Milliseconds ([int][Math]::Max(1, [Math]::Min($PollMilliseconds, $remaining)))
    }

    if ($null -ne $clientFailure) {
        $clientStopped = Stop-OwnedProcess $clientProcess 'client'
        throw $clientFailure
    }

    # The client ended on its own. The host must still have been running when it did: a host that
    # exited first is a failure even if both processes eventually report exit code 0.
    try { $clientExit = $clientProcess.ExitCode }
    catch { $clientExit = $null }
    if ($unityProcess.HasExited) {
        throw "The session host exited (code $($unityProcess.ExitCode)) before the client finished (client exit $clientExit); the host must outlive its client."
    }
    if ($null -eq $clientExit) {
        throw 'The client process ended without an exit code, so the run cannot be reported clean.'
    }
    if ($clientExit -ne 0) {
        throw "The client failed (exit $clientExit); the host is being shut down and the control directory keeps the records."
    }

    Write-Host "Client exited with code $clientExit."
    $exitCode = $ExitClean
}
catch {
    if ($setupFailure) {
        [Console]::Error.WriteLine('SETUP ERROR: ' + $_.Exception.Message)
        $exitCode = $ExitSetup
    }
    else {
        [Console]::Error.WriteLine('ERROR: ' + $_.Exception.Message)
        $exitCode = $ExitFailed
    }
}
finally {
    # Out-of-band supervisor cleanup, not an S1 choice. It runs after a failure, a deadline and a
    # hung client too. Only the client this script started is stopped here; an unrelated python or
    # Unity process on the machine is never touched.
    if ($null -ne $clientProcess) {
        if ($clientStopped -or (Stop-OwnedProcess $clientProcess 'client')) {
            $clientStopped = $true
        }
        Complete-OwnedClient $clientHandle
        try { $clientProcess.Dispose() } catch { }
    }

    if ($unityStarted -and $null -ne $unityProcess) {
        # The host leaves Play Mode and exits itself on the shutdown marker.
        $shutdownPath = Join-Path $control $ShutdownName
        try {
            if (-not [IO.File]::Exists($shutdownPath)) {
                [IO.File]::WriteAllText($shutdownPath, '', (New-Object Text.UTF8Encoding($false)))
            }
        }
        catch {
            Write-Warning "Could not publish the shutdown marker: $($_.Exception.Message)"
        }

        $now = [DateTime]::UtcNow
        $shutdownBy = $now.AddSeconds([Math]::Max(1, $ShutdownTimeoutSeconds))
        if ($shutdownBy -gt $deadlineUtc) { $shutdownBy = $deadlineUtc }
        $waitMilliseconds = [int][Math]::Max(1000, ($shutdownBy - $now).TotalMilliseconds)
        if (-not $unityProcess.WaitForExit($waitMilliseconds)) {
            $unityKilled = $true
            Write-Warning "The session host did not exit within the shutdown window; stopping only the host process this run started (pid $($unityProcess.Id))."
            Stop-Process -Id $unityProcess.Id -Force -ErrorAction SilentlyContinue
            [void]$unityProcess.WaitForExit(30000)
        }

        if ($unityKilled -or ($unityProcess.HasExited -and $unityProcess.ExitCode -ne 0 -and $exitCode -eq $ExitClean)) {
            $exitCode = $ExitFailed
        }

        if ($unityProcess.HasExited) {
            Write-Host "Session host exited with code $($unityProcess.ExitCode)."
        }

        $finalState = Read-HostState $hostStatePath
        if ($null -ne $finalState) { Write-Host "Host state:  $($finalState.phase)" }
        try { $unityProcess.Dispose() } catch { }
    }

    # The evidence is printed after both children have stopped and the client's streams have
    # drained, so a failing run shows the complete bounded tail rather than a partial one.
    if ($exitCode -eq $ExitFailed) {
        Write-LogTail $LogPath
        Write-FileTail -Path $clientStderrPath -Label 'client stderr'
        Write-FileTail -Path $clientStdoutPath -Label 'client stdout'
    }

    if ($mutexHeld -and $null -ne $mutex) {
        try { [void]$mutex.ReleaseMutex() }
        catch { [Console]::Error.WriteLine('WARNING: could not release the bridge test mutex: ' + $_.Exception.Message) }
    }
    if ($null -ne $mutex) {
        try { $mutex.Dispose() } catch { }
    }
}

exit $exitCode
