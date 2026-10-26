<#
.SYNOPSIS
Runs the bridge EditMode tests in a Unity project, in batch mode.

.DESCRIPTION
Validates the project and the selected editor, points UPA_REPO_ROOT at this
repository, takes the machine-wide bridge test mutex, runs Unity with
-runTests -testPlatform EditMode, then reads the NUnit XML result.

The mutex serializes bridge test runs across processes. It is waited for up
to -MutexTimeoutMinutes (default 15), and a mutex abandoned by a killed run is
taken over. This script never passes -quit with -runTests.

Exit codes:
  0  the run finished and reported Passed with failed=0
  1  the run happened but did not pass, or wrote no usable results
  2  setup failed and Unity was not started

.PARAMETER ProjectPath
Unity project root. It must contain Packages/manifest.json and
ProjectSettings/ProjectVersion.txt.

.PARAMETER UnityPath
Unity.exe to run. Defaults to $env:UNITY_EDITOR, then
C:\Program Files\Unity\Hub\Editor\6000.5.4f1\Editor\Unity.exe.

.PARAMETER TestFilter
Passed through as Unity's -testFilter when supplied.

.PARAMETER ResultsPath
NUnit XML target. Defaults to
<repo>/runs/bridge-tests/<project>/editmode-results.xml. A relative path is
resolved against the project. Keep it outside <project>/Temp: Unity clears
that directory on exit and the result file disappears.

.PARAMETER LogPath
Unity log target. Defaults to <repo>/runs/bridge-tests/<project>/editmode.log.
A relative path is resolved against the project.

.PARAMETER MutexTimeoutMinutes
How long to wait for another bridge test run. Default 15.

.EXAMPLE
powershell -File scripts/unity/run-editmode-tests.ps1 -ProjectPath "C:\path\to\project"

.EXAMPLE
powershell -File scripts/unity/run-editmode-tests.ps1 -ProjectPath "C:\path\to\project" -TestFilter "Playtest.Bridge.Tests"

.NOTES
One Editor per project: close any interactive Editor before running. This
script refuses to start when <ProjectPath>/Temp/UnityLockfile exists.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectPath,

    [string]$UnityPath,

    [string]$TestFilter,

    [string]$ResultsPath,

    [string]$LogPath,

    [int]$MutexTimeoutMinutes = 15
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$DefaultUnityPath = 'C:\Program Files\Unity\Hub\Editor\6000.5.4f1\Editor\Unity.exe'
$MutexNames = @(
    'Global\unity-playtest-agent-editmode-tests',
    'Local\unity-playtest-agent-editmode-tests'
)
$VersionPattern = '^\d+\.\d+\.\d+[abfp]\d+$'
$SetupFailure = $false

function Stop-Setup {
    param([string]$Message)
    $script:SetupFailure = $true
    throw $Message
}

function Read-ProjectVersion {
    param([string]$Path)
    $text = [IO.File]::ReadAllText($Path)
    $version = $null
    $revisionVersion = $null
    $revision = $null
    foreach ($line in [regex]::Split($text, '\r?\n')) {
        if ($line -match '^\s*m_EditorVersion:\s*(?<value>\S+)\s*$') {
            $version = $Matches['value']
        }
        elseif ($line -match '^\s*m_EditorVersionWithRevision:\s*(?<value>\S+)\s*\(\s*(?<rev>[0-9a-fA-F]+)\s*\)\s*$') {
            $revisionVersion = $Matches['value']
            $revision = $Matches['rev'].ToLowerInvariant()
        }
    }
    return [pscustomobject]@{
        Version         = $version
        RevisionVersion = $revisionVersion
        Revision        = $revision
    }
}

function Get-EditorFolderVersion {
    param([string]$UnityExePath)
    $editorDirectory = [IO.Path]::GetDirectoryName($UnityExePath)
    if ([string]::Equals([IO.Path]::GetFileName($editorDirectory), 'Editor', [StringComparison]::OrdinalIgnoreCase)) {
        $versionDirectory = [IO.Path]::GetDirectoryName($editorDirectory)
    }
    else {
        $versionDirectory = $editorDirectory
    }
    return [IO.Path]::GetFileName($versionDirectory)
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

function Read-BridgeTestResults {
    param([string]$Path)
    $document = New-Object System.Xml.XmlDocument
    try { $document.Load($Path) }
    catch { throw "The results file is not valid XML: $Path ($($_.Exception.Message))" }
    $run = $document.SelectSingleNode('/test-run')
    if ($null -eq $run) {
        $root = $document.DocumentElement
        if ($null -ne $root -and $root.Name -eq 'test-run') { $run = $root }
    }
    if ($null -eq $run) {
        throw "The results file has no <test-run> root: $Path"
    }
    $values = @{}
    foreach ($name in @('result', 'total', 'passed', 'failed', 'skipped')) {
        $raw = $run.GetAttribute($name)
        if ([string]::IsNullOrWhiteSpace($raw)) { $values[$name] = $null }
        else { $values[$name] = $raw.Trim() }
    }
    return [pscustomobject]@{
        Result  = $values['result']
        Total   = $values['total']
        Passed  = $values['passed']
        Failed  = $values['failed']
        Skipped = $values['skipped']
    }
}

function ConvertTo-Count {
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    $value = 0
    if ([int]::TryParse($Text, [ref]$value)) { return $value }
    return $null
}

function Format-Count {
    param($Value)
    if ($null -eq $Value) { return 'unknown' }
    return [string]$Value
}

function ConvertTo-QuotedArgument {
    param([string]$Value)
    if ([string]::IsNullOrEmpty($Value)) { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Write-LogTail {
    param([string]$Path, [int]$LineCount = 40)
    if (-not [IO.File]::Exists($Path)) {
        Write-Host "Log: (not written) $Path"
        return
    }
    Write-Host "--- last $LineCount log lines: $Path ---"
    try {
        Get-Content -LiteralPath $Path -Tail $LineCount -ErrorAction Stop | ForEach-Object { Write-Host $_ }
    }
    catch {
        Write-Host "(log could not be read: $($_.Exception.Message))"
    }
    Write-Host '--- end of log ---'
}

$exitCode = 2
$mutex = $null
$mutexHeld = $false
$mutexName = $null
$unityRan = $false
$unityRan = $false

try {
    if (-not (Test-Path -LiteralPath $ProjectPath -PathType Container)) {
        Stop-Setup "Unity project not found: $ProjectPath"
    }
    $projectFull = (Resolve-Path -LiteralPath $ProjectPath).ProviderPath

    $manifestPath = Join-Path $projectFull 'Packages\manifest.json'
    $projectVersionPath = Join-Path $projectFull 'ProjectSettings\ProjectVersion.txt'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        Stop-Setup "Not a Unity project (no Packages/manifest.json): $projectFull"
    }
    if (-not (Test-Path -LiteralPath $projectVersionPath -PathType Leaf)) {
        Stop-Setup "Not a Unity project (no ProjectSettings/ProjectVersion.txt): $projectFull"
    }

    if ([string]::IsNullOrWhiteSpace($UnityPath)) {
        $unityFromEnvironment = [Environment]::GetEnvironmentVariable('UNITY_EDITOR')
        if ([string]::IsNullOrWhiteSpace($unityFromEnvironment)) { $UnityPath = $DefaultUnityPath }
        else { $UnityPath = $unityFromEnvironment }
    }
    if (-not (Test-Path -LiteralPath $UnityPath -PathType Leaf)) {
        Stop-Setup "Unity editor not found: $UnityPath (pass -UnityPath or set UNITY_EDITOR)"
    }
    $unityFull = (Resolve-Path -LiteralPath $UnityPath).ProviderPath

    $projectVersion = Read-ProjectVersion $projectVersionPath
    if ([string]::IsNullOrWhiteSpace($projectVersion.Version)) {
        Stop-Setup "ProjectVersion.txt has no m_EditorVersion: $projectVersionPath"
    }
    if ($projectVersion.Version -notmatch $VersionPattern) {
        Stop-Setup "ProjectVersion.txt has an unexpected m_EditorVersion '$($projectVersion.Version)'."
    }
    if (-not [string]::IsNullOrWhiteSpace($projectVersion.RevisionVersion) -and
        -not [string]::Equals($projectVersion.RevisionVersion, $projectVersion.Version, [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Setup "ProjectVersion.txt disagrees with itself: m_EditorVersion '$($projectVersion.Version)' but m_EditorVersionWithRevision names '$($projectVersion.RevisionVersion)'."
    }

    $editorFolderVersion = Get-EditorFolderVersion $unityFull
    if ($editorFolderVersion -notmatch $VersionPattern) {
        Stop-Setup "Cannot read a Unity version from the editor path '$unityFull' (folder '$editorFolderVersion'); pass -UnityPath inside <version>\Editor\."
    }
    if (-not [string]::Equals($editorFolderVersion, $projectVersion.Version, [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Setup "Unity version mismatch: the project needs $($projectVersion.Version) but the selected editor is $editorFolderVersion ($unityFull)."
    }

    $productVersion = (Get-Item -LiteralPath $unityFull -ErrorAction Stop).VersionInfo.ProductVersion
    if (-not [string]::IsNullOrWhiteSpace($productVersion) -and
        $productVersion -match '^(?<version>\d+\.\d+\.\d+[abfp]\d+)(?:_(?<revision>[0-9a-fA-F]+))?$') {
        $productVersionName = $Matches['version']
        $productRevision = $Matches['revision']
        if (-not [string]::Equals($productVersionName, $projectVersion.Version, [StringComparison]::OrdinalIgnoreCase)) {
            Stop-Setup "Unity version mismatch: the project needs $($projectVersion.Version) but the selected editor reports $productVersionName ($unityFull)."
        }
        if (-not [string]::IsNullOrWhiteSpace($productRevision) -and
            -not [string]::IsNullOrWhiteSpace($projectVersion.Revision) -and
            -not [string]::Equals($productRevision, $projectVersion.Revision, [StringComparison]::OrdinalIgnoreCase)) {
            Stop-Setup "Unity revision mismatch: the project needs revision $($projectVersion.Revision) but the selected editor is $productRevision ($unityFull)."
        }
    }

    $tempDirectory = Join-Path $projectFull 'Temp'
    $editorLock = Join-Path $tempDirectory 'UnityLockfile'
    if ([IO.File]::Exists($editorLock)) {
        Stop-Setup "The project looks open in a Unity Editor ('$editorLock' exists). Close that Editor, or delete the stale lock file, then retry."
    }

    $repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
    $fixturesRoot = Join-Path $repoRoot 'fixtures\contracts'
    if (-not (Test-Path -LiteralPath $fixturesRoot -PathType Container)) {
        Stop-Setup "UPA_REPO_ROOT would be '$repoRoot' but '$fixturesRoot' is missing; keep this script at scripts/unity/ inside the repository."
    }
    $env:UPA_REPO_ROOT = $repoRoot

    try { $projectManifest = [IO.File]::ReadAllText($manifestPath) | ConvertFrom-Json }
    catch { Stop-Setup "Packages/manifest.json is not valid JSON: $($_.Exception.Message)" }
    if ($null -eq $projectManifest -or $projectManifest -isnot [System.Management.Automation.PSCustomObject]) {
        Stop-Setup "Packages/manifest.json must contain a JSON object: $manifestPath"
    }
    $dependencyValue = $null
    $dependenciesProperty = $projectManifest.PSObject.Properties['dependencies']
    if ($null -ne $dependenciesProperty -and $null -ne $dependenciesProperty.Value) {
        $dependencyEntry = $dependenciesProperty.Value.PSObject.Properties['com.playtest.bridge']
        if ($null -ne $dependencyEntry) { $dependencyValue = [string]$dependencyEntry.Value }
    }
    if ([string]::IsNullOrWhiteSpace($dependencyValue)) {
        Write-Warning 'Packages/manifest.json does not reference com.playtest.bridge; this run will not exercise the bridge. Run scripts/unity/wire-fpskit.ps1 first.'
    }
    elseif ($dependencyValue -notlike 'file:*') {
        Write-Warning "com.playtest.bridge is '$dependencyValue', not a file: dependency; the run may use a different copy of the package."
    }
    $testableNames = @()
    $testablesProperty = $projectManifest.PSObject.Properties['testables']
    if ($null -ne $testablesProperty -and $null -ne $testablesProperty.Value) {
        $testableNames = @($testablesProperty.Value | ForEach-Object { [string]$_ })
    }
    if (-not ($testableNames -contains 'com.playtest.bridge')) {
        Write-Warning 'Packages/manifest.json does not list com.playtest.bridge in testables; its EditMode tests are not compiled. Run scripts/unity/wire-fpskit.ps1 first.'
    }

    if ($MutexTimeoutMinutes -lt 0) {
        Stop-Setup 'MutexTimeoutMinutes cannot be negative.'
    }

    if ([string]::IsNullOrWhiteSpace($ResultsPath)) {
        $runDirectory = Join-Path (Join-Path $repoRoot 'runs\bridge-tests') (Split-Path $projectFull -Leaf)
        $ResultsPath = Join-Path $runDirectory 'editmode-results.xml'
    }
    if ([string]::IsNullOrWhiteSpace($LogPath)) {
        $runDirectory = Join-Path (Join-Path $repoRoot 'runs\bridge-tests') (Split-Path $projectFull -Leaf)
        $LogPath = Join-Path $runDirectory 'editmode.log'
    }
    if (-not [IO.Path]::IsPathRooted($ResultsPath)) { $ResultsPath = Join-Path $projectFull $ResultsPath }
    if (-not [IO.Path]::IsPathRooted($LogPath)) { $LogPath = Join-Path $projectFull $LogPath }
    $ResultsPath = [IO.Path]::GetFullPath($ResultsPath)
    $LogPath = [IO.Path]::GetFullPath($LogPath)
    if ([string]::Equals($ResultsPath, $LogPath, [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Setup 'ResultsPath and LogPath must be different files.'
    }
    foreach ($outputPath in @($ResultsPath, $LogPath)) {
        $outputDirectory = [IO.Path]::GetDirectoryName($outputPath)
        if (-not [IO.Directory]::Exists($outputDirectory)) {
            try { [void][IO.Directory]::CreateDirectory($outputDirectory) }
            catch { Stop-Setup "Cannot create the output directory '$outputDirectory': $($_.Exception.Message)" }
        }
    }

    Write-Host "Project:     $projectFull"
    Write-Host "Unity:       $unityFull ($editorFolderVersion)"
    Write-Host "Repo root:   $repoRoot  (UPA_REPO_ROOT)"
    Write-Host "Results:     $ResultsPath"
    Write-Host "Log:         $LogPath"
    if (-not [string]::IsNullOrWhiteSpace($TestFilter)) { Write-Host "Filter:      $TestFilter" }

    $mutexHandle = New-BridgeTestMutex
    $mutex = $mutexHandle.Mutex
    $mutexName = $mutexHandle.Name
    Write-Host "Mutex:       $mutexName (waiting up to $MutexTimeoutMinutes minute(s))"
    $acquired = $false
    try {
        $acquired = $mutex.WaitOne([TimeSpan]::FromMinutes([double]$MutexTimeoutMinutes))
    }
    catch [System.Threading.AbandonedMutexException] {
        $acquired = $true
        Write-Warning 'The previous bridge test run abandoned the mutex; continuing as the owner.'
    }
    if (-not $acquired) {
        Stop-Setup "Timed out after $MutexTimeoutMinutes minute(s) waiting for another bridge test run to finish (mutex '$mutexName')."
    }
    $mutexHeld = $true

    foreach ($outputPath in @($ResultsPath, $LogPath)) {
        if ([IO.File]::Exists($outputPath)) {
            try { [IO.File]::Delete($outputPath) }
            catch { Stop-Setup "Cannot replace the stale output '$outputPath': $($_.Exception.Message)" }
        }
    }

    $unityArguments = @(
        '-batchmode',
        '-nographics',
        '-projectPath', $projectFull,
        '-runTests',
        '-testPlatform', 'EditMode',
        '-testResults', $ResultsPath,
        '-logFile', $LogPath
    )
    if (-not [string]::IsNullOrWhiteSpace($TestFilter)) {
        $unityArguments += @('-testFilter', $TestFilter)
    }
    Write-Host ("Running:     `"$unityFull`" " + ($unityArguments -join ' '))
    $startedAt = Get-Date
    $argumentLine = ($unityArguments | ForEach-Object { ConvertTo-QuotedArgument ([string]$_) }) -join ' '
    # Unity.exe is a GUI-subsystem binary; the call operator returns immediately for those.
    # Start-Process -Wait is what actually blocks until the test run exits, and its ExitCode
    # replaces $LASTEXITCODE (which the call operator never sets here).
    $unityProcess = Start-Process -FilePath $unityFull -ArgumentList $argumentLine -NoNewWindow -Wait -PassThru
    $unityExitCode = $unityProcess.ExitCode
    $elapsed = (Get-Date) - $startedAt
    $unityRan = $true
    Write-Host ("Unity exited after {0:N1}s with code {1}." -f $elapsed.TotalSeconds, $unityExitCode)

    if ([IO.File]::Exists($ResultsPath)) {
        Write-Host "Results:     $ResultsPath"
    }
    else {
        Write-Host "Results:     $ResultsPath (missing)"
    }
    Write-Host "Log:         $LogPath"

    if (-not [IO.File]::Exists($ResultsPath)) {
        Write-Host 'FAIL: Unity wrote no test results, so this run is not a pass.' -ForegroundColor Red
        Write-LogTail $LogPath
        $exitCode = 1
    }
    else {
        $results = Read-BridgeTestResults $ResultsPath
        $totalCount = ConvertTo-Count $results.Total
        $passedCount = ConvertTo-Count $results.Passed
        $failedCount = ConvertTo-Count $results.Failed
        $skippedCount = ConvertTo-Count $results.Skipped
        $resultName = $results.Result
        if ([string]::IsNullOrWhiteSpace($resultName)) { $resultName = 'unknown' }
        Write-Host ("EditMode tests: {0}  total={1}  passed={2}  failed={3}  skipped={4}" -f `
                $resultName, (Format-Count $totalCount), (Format-Count $passedCount), (Format-Count $failedCount), (Format-Count $skippedCount))

        if (($null -ne $failedCount) -and $results.Result -eq 'Passed' -and $failedCount -eq 0) {
            if ($null -eq $totalCount) {
                Write-Host 'FAIL: the results file does not report a total; a pass cannot be inferred.' -ForegroundColor Red
                $exitCode = 1
            }
            elseif ($totalCount -eq 0) {
                Write-Host 'FAIL: the run reported zero tests. That is not evidence; check the manifest dependency and testables entry.' -ForegroundColor Red
                $exitCode = 1
            }
            else {
                Write-Host 'PASS: EditMode tests passed.' -ForegroundColor Green
                $exitCode = 0
            }
        }
        else {
            if ($null -eq $failedCount) {
                Write-Host 'FAIL: the results file has no usable failed count.' -ForegroundColor Red
            }
            else {
                Write-Host ("FAIL: result is '{0}' with {1} failure(s)." -f $resultName, $failedCount) -ForegroundColor Red
            }
            Write-LogTail $LogPath
            $exitCode = 1
        }
    }
}
catch {
    if ($SetupFailure) {
        [Console]::Error.WriteLine('SETUP ERROR: ' + $_.Exception.Message)
        $exitCode = 2
    }
    else {
        [Console]::Error.WriteLine('ERROR: ' + $_.Exception.Message)
        if ($unityRan) { Write-LogTail $LogPath }
        $exitCode = 1
    }
}
finally {
    if ($mutexHeld -and $null -ne $mutex) {
        try { [void]$mutex.ReleaseMutex() }
        catch { [Console]::Error.WriteLine('WARNING: could not release the bridge test mutex: ' + $_.Exception.Message) }
    }
    if ($null -ne $mutex) {
        try { $mutex.Dispose() } catch { }
    }
}

exit $exitCode
