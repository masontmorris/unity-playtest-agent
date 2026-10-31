<#
.SYNOPSIS
Process-level tests for play-session-supervisor.ps1, using fake Unity and Python processes.

.DESCRIPTION
The supervisor's own job is process supervision, so these tests give it real child processes with
the same shape as the live ones: a fake executable plays the Editor host when it receives
`-executeMethod`, and the Python client when it receives `-m`. Each scenario writes a fresh project,
control directory and config, runs the supervisor in a child PowerShell process, and measures the
exit code, the run-local evidence and which processes are still alive.

They launch no Unity Editor, make no model call and touch no repository file outside a temporary
directory. The fake program is copied there and built once; any bin/ or obj/ output stays there.

Covered:
  - normal completion, with the host outliving the client and reacting to the shutdown marker
  - a hung client bounded by the validated absolute config deadline
  - the host exiting while the client is still running
  - a missing, naive, malformed or already-passed deadline (setup error, Unity never starts)
  - project, control and output paths containing spaces, and a quoted argument value
  - cleanup that stops only the processes this run started
  - a host that ignores the shutdown marker and has to be stopped
#>
[CmdletBinding()]
param(
    [string]$FakeSourceDirectory,
    [switch]$KeepTemporaryDirectory
)

$ErrorActionPreference = 'Stop'
$invariant = [Globalization.CultureInfo]::InvariantCulture
$script:failures = 0

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$supervisor = Join-Path $PSScriptRoot 'play-session-supervisor.ps1'
$workRoot = Join-Path ([IO.Path]::GetTempPath()) ('upa-supervisor-tests-' + [guid]::NewGuid().ToString('N'))
$scenarioRoots = New-Object Collections.Generic.List[string]

if ([string]::IsNullOrWhiteSpace($FakeSourceDirectory)) {
    $FakeSourceDirectory = Join-Path $PSScriptRoot 'tests/fake_session_process'
}
$FakeSourceDirectory = [IO.Path]::GetFullPath($FakeSourceDirectory)
if (-not [IO.File]::Exists((Join-Path $FakeSourceDirectory 'FakeSessionProcess.csproj'))) {
    throw "The fake process project is missing from $FakeSourceDirectory."
}

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

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw "ASSERTION FAILED: $Message" }
    Write-Host "  ok: $Message"
}

function Assert-Equal {
    param($Expected, $Actual, [string]$Message)
    if ($Expected -ne $Actual) {
        throw "ASSERTION FAILED: $Message (expected '$Expected', got '$Actual')"
    }
    Write-Host "  ok: $Message"
}

function Assert-Contains {
    param([string]$Text, [string]$Expected, [string]$Message)
    if ($Text -notlike "*$Expected*") {
        throw "ASSERTION FAILED: $Message (missing '$Expected')`n--- text ---`n$Text"
    }
    Write-Host "  ok: $Message"
}

function Write-JsonFile {
    param($Value, [string]$Path)
    [IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Compress), (New-Object Text.UTF8Encoding($false)))
}

function New-Deadline {
    param([int]$Seconds = 120)
    return [DateTime]::UtcNow.AddSeconds($Seconds).ToString("yyyy-MM-dd'T'HH:mm:ss.fffffff'Z'", $invariant)
}

function New-Scenario {
    param(
        [string]$Name,
        [string]$DeadlineUtc,
        [switch]$OmitDeadline
    )

    $root = Join-Path $workRoot ($Name + '-dir with spaces')
    $project = Join-Path $root 'project dir'
    $control = Join-Path $root 'control dir'
    $outputDir = Join-Path $root 'client output'
    New-Item -ItemType Directory -Path (Join-Path $project 'Assets') -Force | Out-Null
    New-Item -ItemType Directory -Path $control -Force | Out-Null
    New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
    $scenarioRoots.Add($root)

    $entity = Join-Path $root 'entity file.json'
    $calibration = Join-Path $root 'calibration file.json'
    $manifest = Join-Path $root 'manifest file.json'
    foreach ($path in @($entity, $calibration, $manifest)) {
        [IO.File]::WriteAllText($path, '{}', (New-Object Text.UTF8Encoding($false)))
    }

    $runId = [guid]::NewGuid().ToString('D')
    if (-not $OmitDeadline -and [string]::IsNullOrWhiteSpace($DeadlineUtc)) {
        $DeadlineUtc = New-Deadline
    }
    $config = [ordered]@{
        version        = 1
        run_id         = $runId
        project_path   = $project
        catalog_path   = (Join-Path $root 'catalog.json')
        catalog_sha256 = ('a' * 64)
    }
    if (-not $OmitDeadline) { $config['deadline_utc'] = $DeadlineUtc }
    Write-JsonFile $config (Join-Path $control 'config.json')

    return [pscustomobject]@{
        Root        = $root
        Project     = $project
        Control     = $control
        Output      = (Join-Path $outputDir 'session summary.json')
        Entity      = $entity
        Calibration = $calibration
        Manifest    = $manifest
        RunId       = $runId
    }
}

function Invoke-Supervisor {
    param(
        $Scenario,
        [string]$UnityMode = 'hold',
        [string]$ClientMode = 'exit0',
        [string]$ClientModule = 'fake.client.module',
        [string]$UnityExitSeconds = '0',
        [string]$ClientHangSeconds = '600',
        [int]$ReadyTimeoutSeconds = 30,
        [int]$ShutdownTimeoutSeconds = 8
    )

    $names = @(
        'UPA_FAKE_UNITY_MODE', 'UPA_FAKE_CLIENT_MODE', 'UPA_FAKE_UNITY_EXIT_SECONDS',
        'UPA_FAKE_CLIENT_HANG_SECONDS'
    )
    $previous = @{}
    foreach ($name in $names) { $previous[$name] = [Environment]::GetEnvironmentVariable($name) }

    $fakeExe = Join-Path $workRoot 'fake-bin/fake-session-process.exe'
    $arguments = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $supervisor,
        '-ProjectPath', $Scenario.Project, '-ControlDirectory', $Scenario.Control,
        '-PythonPath', $fakeExe, '-ClientModule', $ClientModule, '-ClientProject', $Scenario.Project,
        '-ClientEntity', $Scenario.Entity, '-ClientCalibration', $Scenario.Calibration,
        '-ClientManifest', $Scenario.Manifest, '-ClientOutput', $Scenario.Output,
        '-UnityPath', $fakeExe, '-LogPath', (Join-Path $Scenario.Control 'unity.log'),
        '-InitialReadyTimeoutSeconds', [string]$ReadyTimeoutSeconds,
        '-ShutdownTimeoutSeconds', [string]$ShutdownTimeoutSeconds,
        '-MutexTimeoutMinutes', '1'
    )
    $argumentLine = (($arguments | ForEach-Object { ConvertTo-QuotedArgument ([string]$_) }) -join ' ')

    try {
        $env:UPA_FAKE_UNITY_MODE = $UnityMode
        $env:UPA_FAKE_CLIENT_MODE = $ClientMode
        $env:UPA_FAKE_UNITY_EXIT_SECONDS = $UnityExitSeconds
        $env:UPA_FAKE_CLIENT_HANG_SECONDS = $ClientHangSeconds

        $clock = [Diagnostics.Stopwatch]::StartNew()
        $startInfo = New-Object Diagnostics.ProcessStartInfo
        $startInfo.FileName = 'powershell.exe'
        $startInfo.Arguments = $argumentLine
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $process = New-Object Diagnostics.Process
        $process.StartInfo = $startInfo
        if (-not $process.Start()) { throw 'Could not start the supervisor process.' }
        $outTask = $process.StandardOutput.ReadToEndAsync()
        $errTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(180000)) {
            try { $process.Kill() } catch { }
            throw 'The supervisor did not finish within 180 seconds.'
        }
        $clock.Stop()
        $exitCode = $process.ExitCode
        $stdoutText = $outTask.Result
        $stderrText = $errTask.Result
    }
    finally {
        foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $previous[$name]) }
    }

    return [pscustomobject]@{
        ExitCode = $exitCode
        Seconds  = $clock.Elapsed.TotalSeconds
        Stdout   = $stdoutText
        Stderr   = $stderrText
    }
}

function Get-FakePid {
    param($Scenario, [string]$Name)
    $path = Join-Path $Scenario.Control ($Name + '.pid')
    if (-not [IO.File]::Exists($path)) { return $null }
    $value = [int]([IO.File]::ReadAllText($path).Trim())
    return $value
}

function Test-OwnedFakeAlive {
    param([int]$Id)
    $process = Get-Process -Id $Id -ErrorAction SilentlyContinue
    return ($null -ne $process -and $process.ProcessName -eq 'fake-session-process')
}

function Test-ProcessAlive {
    param([int]$Id)
    return ($null -ne (Get-Process -Id $Id -ErrorAction SilentlyContinue))
}

function Stop-FakeProcesses {
    foreach ($root in $scenarioRoots) {
        Get-ChildItem -LiteralPath $root -Recurse -Filter 'fake-*.pid' -ErrorAction SilentlyContinue |
            ForEach-Object {
                $id = 0
                if ([int]::TryParse((Get-Content -LiteralPath $_.FullName -Raw).Trim(), [ref]$id)) {
                    if (Test-OwnedFakeAlive $id) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
                }
            }
    }
}

function Invoke-Scenario {
    param([string]$Name, [scriptblock]$Body)
    Write-Host ""
    Write-Host "== $Name =="
    try {
        & $Body
        Write-Host "PASS: $Name"
    }
    catch {
        $script:failures++
        Write-Host ("FAIL: {0}`n{1}" -f $Name, $_.Exception.Message)
    }
    finally {
        Stop-FakeProcesses
    }
}

New-Item -ItemType Directory -Path $workRoot -Force | Out-Null
$buildSource = Join-Path $workRoot 'fake-src'
New-Item -ItemType Directory -Path $buildSource -Force | Out-Null
Copy-Item -Path (Join-Path $FakeSourceDirectory '*') -Destination $buildSource -Force
$fakeOut = Join-Path $workRoot 'fake-bin'
Write-Host "Building the fake session process in $fakeOut"
& dotnet build (Join-Path $buildSource 'FakeSessionProcess.csproj') -c Release -o $fakeOut --nologo -v quiet
if ($LASTEXITCODE -ne 0 -or -not [IO.File]::Exists((Join-Path $fakeOut 'fake-session-process.exe'))) {
    throw 'Could not build the fake session process.'
}

try {
    Invoke-Scenario 'normal completion' {
        $scenario = New-Scenario 'normal'
        $result = Invoke-Supervisor -Scenario $scenario -UnityMode 'hold' -ClientMode 'exit0'

        Assert-Equal 0 $result.ExitCode 'the client and the host finish cleanly'
        Assert-True ([IO.File]::Exists((Join-Path $scenario.Control 'shutdown'))) 'the shutdown marker is published'
        Assert-Equal 'stopped' (([IO.File]::ReadAllText((Join-Path $scenario.Control 'host-state.json')) | ConvertFrom-Json).phase) 'the host published its stopped phase'
        Assert-Contains ([IO.File]::ReadAllText((Join-Path $scenario.Control 'client.stdout.log'))) 'fake client: mode exit0' 'client stdout is kept as run-local evidence'
        Assert-Contains ([IO.File]::ReadAllText((Join-Path $scenario.Control 'client.stderr.log'))) 'fake client stderr' 'client stderr is kept as run-local evidence'
        Assert-True (-not (Test-OwnedFakeAlive (Get-FakePid $scenario 'fake-client'))) 'the client is gone'
        Assert-True (-not (Test-OwnedFakeAlive (Get-FakePid $scenario 'fake-unity'))) 'the host is gone'
        Assert-True ($result.Seconds -lt 60) 'the normal run finishes promptly'
    }

    Invoke-Scenario 'hung client bounded by the deadline' {
        $scenario = New-Scenario 'hung-client' -DeadlineUtc (New-Deadline 4)
        $result = Invoke-Supervisor -Scenario $scenario -UnityMode 'hold' -ClientMode 'hang' `
            -ReadyTimeoutSeconds 30 -ShutdownTimeoutSeconds 5

        Assert-Equal 1 $result.ExitCode 'a hung client fails the run'
        Assert-Contains $result.Stderr 'deadline' 'the failure names the session deadline'
        Assert-True (-not (Test-OwnedFakeAlive (Get-FakePid $scenario 'fake-client'))) 'the supervisor stopped its hung client'
        Assert-True (-not (Test-OwnedFakeAlive (Get-FakePid $scenario 'fake-unity'))) 'the host was shut down after the client'
        Assert-True ([IO.File]::Exists((Join-Path $scenario.Control 'shutdown'))) 'cleanup still published the shutdown marker'
        Assert-True ($result.Seconds -lt 30) 'the whole run is bounded by the deadline, not the client'
    }

    Invoke-Scenario 'host exits while the client still runs' {
        $scenario = New-Scenario 'host-exit' -DeadlineUtc (New-Deadline 60)
        $result = Invoke-Supervisor -Scenario $scenario -UnityMode 'exit-after-ready' -ClientMode 'hang' `
            -UnityExitSeconds '2' -ReadyTimeoutSeconds 30 -ShutdownTimeoutSeconds 5

        Assert-Equal 1 $result.ExitCode 'a host that exits first fails even with exit code 0'
        Assert-Contains $result.Stderr 'host exited' 'the failure names the early host exit'
        Assert-True (-not (Test-OwnedFakeAlive (Get-FakePid $scenario 'fake-client'))) 'the hung client was stopped'
        Assert-True ($result.Seconds -lt 30) 'the supervisor does not wait for the client deadline'
    }

    Invoke-Scenario 'host exits before a client that would have exited cleanly' {
        $scenario = New-Scenario 'host-exit-clean-client' -DeadlineUtc (New-Deadline 60)
        $result = Invoke-Supervisor -Scenario $scenario -UnityMode 'exit-after-ready' -ClientMode 'hang-then-exit0' `
            -UnityExitSeconds '1' -ClientHangSeconds '30' -ReadyTimeoutSeconds 30 -ShutdownTimeoutSeconds 5

        Assert-Equal 1 $result.ExitCode 'the run fails although the client would have reported 0'
        Assert-Contains $result.Stderr 'host exited' 'the host exit is the reason, not the client code'
    }

    Invoke-Scenario 'invalid or missing deadlines are setup errors' {
        $cases = @(
            @{ Name = 'deadline-missing'; Config = @{ Omit = $true }; Expect = 'deadline_utc' },
            @{ Name = 'deadline-naive'; Config = @{ Value = '2027-01-02T03:04:05' }; Expect = 'deadline_utc' },
            @{ Name = 'deadline-malformed'; Config = @{ Value = 'not-an-instant' }; Expect = 'deadline_utc' },
            @{ Name = 'deadline-past'; Config = @{ Value = (New-Deadline -Seconds -300) }; Expect = 'already passed' }
        )
        foreach ($case in $cases) {
            if ($case.Config.Omit) {
                $scenario = New-Scenario $case.Name -OmitDeadline
            }
            else {
                $scenario = New-Scenario $case.Name -DeadlineUtc $case.Config.Value
            }

            $result = Invoke-Supervisor -Scenario $scenario -ClientMode 'exit0'
            Assert-Equal 2 $result.ExitCode "$($case.Name) is a setup failure"
            Assert-Contains $result.Stderr $case.Expect "$($case.Name) names the deadline"
            Assert-True (-not [IO.File]::Exists((Join-Path $scenario.Control 'fake-unity.pid'))) "$($case.Name) never started Unity"
        }
    }

    Invoke-Scenario 'paths with spaces and quoted argument values survive' {
        $scenario = New-Scenario 'spaces' -DeadlineUtc (New-Deadline 60)
        $module = 'fake "quoted" module'
        $result = Invoke-Supervisor -Scenario $scenario -ClientMode 'exit0' -ClientModule $module

        Assert-Equal 0 $result.ExitCode 'the run completes with spaced paths'
        $arguments = [IO.File]::ReadAllLines((Join-Path $scenario.Control 'fake-client.args'))
        Assert-Equal '-m' $arguments[0] 'the client receives its module flag'
        Assert-Equal $module $arguments[1] 'a value with a space and quotes reaches the client unchanged'
        Assert-Equal $scenario.Project $arguments[3] 'the project path with spaces reaches the client unchanged'
        $outputIndex = [Array]::IndexOf($arguments, '--output')
        Assert-True ($outputIndex -ge 0) 'the output flag is present'
        Assert-Equal $scenario.Output $arguments[$outputIndex + 1] 'the output path with spaces reaches the client unchanged'
    }

    Invoke-Scenario 'cleanup stops only the processes this run started' {
        $scenario = New-Scenario 'ownership' -DeadlineUtc (New-Deadline 3)
        $sentinel = Start-Process -FilePath 'powershell.exe' `
            -ArgumentList '-NoProfile -Command "Start-Sleep -Seconds 120"' -WindowStyle Hidden -PassThru
        try {
            $result = Invoke-Supervisor -Scenario $scenario -UnityMode 'hold' -ClientMode 'hang' `
                -ReadyTimeoutSeconds 30 -ShutdownTimeoutSeconds 5

            Assert-Equal 1 $result.ExitCode 'the run fails on the deadline'
            Assert-True (Test-ProcessAlive $sentinel.Id) 'an unrelated process is still running'
            Assert-True (-not (Test-OwnedFakeAlive (Get-FakePid $scenario 'fake-client'))) 'the run''s client was stopped'
            Assert-True (-not (Test-OwnedFakeAlive (Get-FakePid $scenario 'fake-unity'))) 'the run''s host was stopped'
        }
        finally {
            if (Test-ProcessAlive $sentinel.Id) { Stop-Process -Id $sentinel.Id -Force -ErrorAction SilentlyContinue }
        }
    }

    Invoke-Scenario 'a host that ignores shutdown is stopped after the bounded wait' {
        $scenario = New-Scenario 'ignores-shutdown' -DeadlineUtc (New-Deadline 60)
        $result = Invoke-Supervisor -Scenario $scenario -UnityMode 'ignore-shutdown' -ClientMode 'exit0' `
            -ReadyTimeoutSeconds 30 -ShutdownTimeoutSeconds 2

        Assert-Equal 1 $result.ExitCode 'a host that does not exit is a failure'
        Assert-True (-not (Test-OwnedFakeAlive (Get-FakePid $scenario 'fake-unity'))) 'the supervisor stopped only its own host'
        Assert-True ($result.Seconds -lt 30) 'the shutdown wait is bounded'
    }
}
finally {
    Stop-FakeProcesses
    if (-not $KeepTemporaryDirectory) {
        try { Remove-Item -LiteralPath $workRoot -Recurse -Force -ErrorAction SilentlyContinue } catch { }
    }
    else {
        Write-Host "Temporary test directory kept at $workRoot"
    }
}

Write-Host ""
if ($script:failures -gt 0) {
    Write-Host "$($script:failures) scenario(s) failed."
    exit 1
}
Write-Host 'All supervisor process scenarios passed.'
exit 0
