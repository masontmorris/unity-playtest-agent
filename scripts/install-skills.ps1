<#
.SYNOPSIS
Installs the committed `.agents/skills` collection as a pinned snapshot and
points per-provider skill directories at it.

.DESCRIPTION
Reads the skill tree from a commit, never from the working tree, and writes it
to `<ReleaseRoot>\<commit>\`. It then repoints one junction per skill in each
provider root. Every step is verified by Git blob hash, and a link is replaced
only after its replacement is staged and checked. On failure the previous
junctions are restored. A link whose target is neither the in-repo path nor the
requested release is refused, so unrelated skills and hand-made links survive.

Use `-WhatIf` to print the planned changes without touching the filesystem.
Re-running against an installed release is a no-op.

.PARAMETER Repo
Root of a Git worktree that contains `.agents/skills`.

.PARAMETER Commit
Full 40-character commit id that contains the skill tree to install.

.PARAMETER ReleaseRoot
Directory that holds one immutable snapshot per commit.

.PARAMETER ProviderRoot
Directories that hold per-skill junction links, for example `~/.codex/skills`
and `~/.claude/skills`. Defaults are the two current Codex and Claude roots.

.EXAMPLE
powershell -File scripts/install-skills.ps1 -Repo . -Commit 73a4637661be50f555946f9bc71fe480df6f24bd -WhatIf

.EXAMPLE
powershell -File scripts/install-skills.ps1 -Repo . -Commit 73a4637661be50f555946f9bc71fe480df6f24bd

.NOTES
Windows only, because it creates directory junctions. Every provider root and
every one of its five links must already exist, pointing either at the in-repo
path or at the target release. On a new machine, create those links once by
hand, then use this script for every later change.
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Low')]
param(
    [Parameter(Mandatory = $true)]
    [string]$Repo,

    [Parameter(Mandatory = $true)]
    [string]$Commit,

    [string]$ReleaseRoot = (Join-Path $HOME '.local\share\unity-playtest-agent\skills'),

    [ValidateCount(1, 16)]
    [string[]]$ProviderRoot = @(
        (Join-Path $HOME '.codex\skills'),
        (Join-Path $HOME '.claude\skills')
    )
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$Subtree = '.agents/skills'
$SubtreePrefix = '.agents/skills/'
$ExpectedSkills = @(
    'playtest-s1-ownership',
    'playtest-contract-change',
    'playtest-preflight-probe',
    'playtest-eval-design',
    'playtest-evidence-review'
)

function Stop-Install {
    param([string]$Message)
    throw $Message
}

function Test-SamePath {
    param([string]$Left, [string]$Right)
    return [string]::Equals($Left, $Right, [StringComparison]::OrdinalIgnoreCase)
}

function Normalize-Path {
    param([string]$Path, [string]$Base)
    if ([string]::IsNullOrWhiteSpace($Path)) { Stop-Install 'Empty path.' }
    $p = $Path.Trim()
    if ($p.StartsWith('\??\')) { $p = $p.Substring(4) }
    if ($p.StartsWith('\\?\')) { $p = $p.Substring(4) }
    if (-not [IO.Path]::IsPathRooted($p)) {
        if ([string]::IsNullOrWhiteSpace($Base)) { Stop-Install "Relative path without a base: $Path" }
        $p = [IO.Path]::Combine($Base, $p)
    }
    $full = [IO.Path]::GetFullPath($p)
    $root = [IO.Path]::GetPathRoot($full)
    if ($full.Length -gt $root.Length) {
        $full = $full.TrimEnd([char[]]@('\', '/'))
    }
    return $full
}

function Test-AnyEntry {
    param([string]$Path)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    return ($null -ne $item)
}

function Assert-RealDirectory {
    param([string]$Path, [string]$Label)
    if (-not (Test-AnyEntry $Path)) { Stop-Install "$Label is missing: $Path" }
    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.PSIsContainer) { Stop-Install "$Label is not a directory: $Path" }
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        Stop-Install "$Label is a reparse point, not a real directory: $Path"
    }
}

function Get-JunctionTarget {
    param([string]$Path, [string]$Label)
    if (-not (Test-AnyEntry $Path)) { Stop-Install "$Label is missing: $Path" }
    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.PSIsContainer) { Stop-Install "$Label is not a directory junction: $Path" }
    if ($item.LinkType -ne 'Junction') {
        Stop-Install "$Label is not a junction (LinkType=$($item.LinkType)): $Path"
    }
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) {
        Stop-Install "$Label is not a reparse point: $Path"
    }
    $raw = @($item.Target)
    if ($raw.Count -ne 1 -or [string]::IsNullOrWhiteSpace([string]$raw[0])) {
        Stop-Install "$Label has an unreadable target: $Path"
    }
    if (-not [IO.Directory]::Exists($Path)) {
        Stop-Install "$Label is dangling: $Path -> $($raw[0])"
    }
    return Normalize-Path ([string]$raw[0]) (Split-Path -Parent $Path)
}

function Get-CommittedInventory {
    param([string]$GitRepo, [string]$FullCommit)
    $lines = @(& git -C $GitRepo ls-tree -r --full-name $FullCommit -- $Subtree 2>$null)
    if ($LASTEXITCODE -ne 0) {
        Stop-Install "git ls-tree failed for $FullCommit"
    }
    $files = New-Object 'System.Collections.Generic.Dictionary[string,object]' ([StringComparer]::OrdinalIgnoreCase)
    $topDirs = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($line in $lines) {
        if ([string]::IsNullOrWhiteSpace([string]$line)) { continue }
        if ([string]$line -notmatch '^(?<mode>[0-9]{6}) (?<type>[a-z]+) (?<blob>[0-9a-f]{40})\t(?<path>.+)$') {
            Stop-Install "Unparseable git ls-tree line: $line"
        }
        $mode = $Matches['mode']
        $type = $Matches['type']
        $blob = $Matches['blob']
        $path = $Matches['path']
        if ($type -ne 'blob') { Stop-Install "Non-blob entry in $Subtree ($type): $path" }
        if ($mode -ne '100644' -and $mode -ne '100755') {
            Stop-Install "Unsupported file mode $mode in ${Subtree}: $path"
        }
        if (-not $path.StartsWith($SubtreePrefix, [StringComparison]::Ordinal)) {
            Stop-Install "Path outside $SubtreePrefix`: $path"
        }
        if ($path -match '[<>:"|?*\x00-\x1F]' -or $path.Contains('\')) {
            Stop-Install "Unsafe or non-portable committed path: $path"
        }
        $rel = $path.Substring($SubtreePrefix.Length)
        $parts = $rel.Split('/')
        foreach ($part in $parts) {
            if ([string]::IsNullOrEmpty($part) -or $part -eq '.' -or $part -eq '..' -or
                $part.EndsWith('.') -or $part.EndsWith(' ')) {
                Stop-Install "Unsafe committed path segment in: $path"
            }
        }
        if ($parts.Count -eq 1) {
            if ($parts[0] -ne 'README.md') { Stop-Install "Unexpected top-level file in $Subtree`: $path" }
        }
        else {
            if ($ExpectedSkills -notcontains $parts[0]) {
                Stop-Install "Unexpected skill directory in $Subtree`: $($parts[0])"
            }
            [void]$topDirs.Add($parts[0])
        }
        if ($files.ContainsKey($path)) { Stop-Install "Duplicate committed path: $path" }
        $files.Add($path, [pscustomobject]@{ Mode = $mode; Blob = $blob })
    }
    if (-not $files.ContainsKey($SubtreePrefix + 'README.md')) {
        Stop-Install "Missing $SubtreePrefix`README.md in commit."
    }
    foreach ($id in $ExpectedSkills) {
        if (-not $files.ContainsKey($SubtreePrefix + $id + '/SKILL.md')) {
            Stop-Install "Missing committed $SubtreePrefix$id/SKILL.md"
        }
    }
    if ($topDirs.Count -ne $ExpectedSkills.Count) {
        Stop-Install "Expected exactly $($ExpectedSkills.Count) skill directories, found $($topDirs.Count)."
    }
    foreach ($id in $ExpectedSkills) {
        if (-not $topDirs.Contains($id)) { Stop-Install "Missing expected skill directory: $id" }
    }
    return [pscustomobject]@{ Files = $files; TopDirs = $topDirs }
}

function Get-ExpectedMap {
    param($Inventory, [string]$Prefix)
    $map = New-Object 'System.Collections.Generic.Dictionary[string,object]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($key in $Inventory.Files.Keys) {
        if (-not $key.StartsWith($Prefix, [StringComparison]::Ordinal)) { continue }
        $rel = $key.Substring($Prefix.Length)
        if ([string]::IsNullOrEmpty($rel)) { Stop-Install "Empty relative path under $Prefix" }
        $map.Add($rel, $Inventory.Files[$key])
    }
    return $map
}

function Get-ExpectedDirs {
    param($Inventory, [string]$Prefix)
    $dirs = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($key in $Inventory.Files.Keys) {
        if (-not $key.StartsWith($Prefix, [StringComparison]::Ordinal)) { continue }
        $rel = $key.Substring($Prefix.Length)
        $parts = $rel.Split('/')
        $acc = $Prefix.TrimEnd('/')
        for ($i = 0; $i -lt ($parts.Count - 1); $i++) {
            $acc = $acc + '/' + $parts[$i]
            [void]$dirs.Add($acc)
        }
    }
    return $dirs
}

function Get-BlobHash {
    param([string]$GitRepo, [string]$Path)
    $out = @(& git -C $GitRepo hash-object --no-filters -- $Path 2>$null)
    if ($LASTEXITCODE -ne 0) { Stop-Install "git hash-object failed for $Path" }
    if ($out.Count -ne 1 -or [string]::IsNullOrWhiteSpace([string]$out[0])) {
        Stop-Install "Unexpected git hash-object output for $Path"
    }
    return ([string]$out[0]).Trim()
}

function Assert-TreeMatches {
    param(
        [string]$Root,
        [string]$Prefix,
        $Inventory,
        [string]$GitRepo,
        [string]$Label
    )
    if (-not [IO.Directory]::Exists($Root)) { Stop-Install "$Label is missing: $Root" }
    $expected = Get-ExpectedMap $Inventory $Prefix
    $expectedDirs = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($key in $expected.Keys) {
        $parts = $key.Split('/')
        $acc = ''
        for ($i = 0; $i -lt ($parts.Count - 1); $i++) {
            $acc = if ($acc) { $acc + '/' + $parts[$i] } else { $parts[$i] }
            [void]$expectedDirs.Add($acc)
        }
    }
    $actual = New-Object 'System.Collections.Generic.Dictionary[string,string]' ([StringComparer]::OrdinalIgnoreCase)
    $actualDirs = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $stack = New-Object 'System.Collections.Generic.Stack[string]'
    $stack.Push($Root)
    while ($stack.Count -gt 0) {
        $dir = $stack.Pop()
        foreach ($entry in [IO.Directory]::GetFileSystemEntries($dir)) {
            $attrs = [IO.File]::GetAttributes($entry)
            if (($attrs -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                Stop-Install "$Label contains a reparse point: $entry"
            }
            if (($attrs -band [IO.FileAttributes]::Directory) -ne 0) {
                $relDir = $entry.Substring($Root.Length).TrimStart('\').Replace('\', '/')
                [void]$actualDirs.Add($relDir)
                $stack.Push($entry)
            }
            else {
                $relFile = $entry.Substring($Root.Length).TrimStart('\').Replace('\', '/')
                if ($actual.ContainsKey($relFile)) { Stop-Install "$Label has duplicate path: $relFile" }
                $actual.Add($relFile, $entry)
            }
        }
    }
    foreach ($key in $expected.Keys) {
        if (-not $actual.ContainsKey($key)) { Stop-Install "$Label is missing committed file: $key" }
    }
    foreach ($key in $actual.Keys) {
        if (-not $expected.ContainsKey($key)) { Stop-Install "$Label has unexpected file: $key" }
    }
    foreach ($dir in $actualDirs) {
        if (-not $expectedDirs.Contains($dir)) { Stop-Install "$Label has unexpected directory: $dir" }
    }
    foreach ($dir in $expectedDirs) {
        if (-not $actualDirs.Contains($dir)) { Stop-Install "$Label is missing committed directory: $dir" }
    }
    foreach ($key in $expected.Keys) {
        $hash = Get-BlobHash $GitRepo $actual[$key]
        if (-not [string]::Equals($hash, $expected[$key].Blob, [StringComparison]::OrdinalIgnoreCase)) {
            Stop-Install "$Label content hash mismatch: $key (found $hash, expected $($expected[$key].Blob))"
        }
    }
}

function Expand-CommittedArchive {
    param(
        [string]$Archive,
        [string]$Destination,
        $Inventory,
        [string]$GitRepo
    )
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $expected = Get-ExpectedMap $Inventory ''
    $expectedDirs = Get-ExpectedDirs $Inventory $SubtreePrefix
    $seenFiles = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $seenDirs = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $destRoot = Normalize-Path $Destination
    $destPrefix = $destRoot.TrimEnd('\') + '\'
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName
            if ([string]::IsNullOrWhiteSpace($name) -or $name.Contains('\') -or $name.Contains(':')) {
                Stop-Install "Unsafe archive entry: [$name]"
            }
            $isDir = $name.EndsWith('/')
            $clean = $name.TrimEnd('/')
            $parts = $clean.Split('/')
            foreach ($part in $parts) {
                if ([string]::IsNullOrEmpty($part) -or $part -eq '.' -or $part -eq '..') {
                    Stop-Install "Archive path escapes the release root: [$name]"
                }
            }
            if ($isDir) {
                if ($clean -eq '.agents' -or $clean -eq $Subtree) { continue }
                if (-not $expectedDirs.Contains($clean) -or -not (@($expectedDirs) -ccontains $clean)) {
                    Stop-Install "Unexpected archive directory: [$name]"
                }
                if (-not $seenDirs.Add($clean)) { Stop-Install "Duplicate archive directory: [$name]" }
                $rel = $clean.Substring($SubtreePrefix.Length)
                [void][IO.Directory]::CreateDirectory((Join-Path $destRoot $rel.Replace('/', '\')))
                continue
            }
            if (-not $clean.StartsWith($SubtreePrefix, [StringComparison]::Ordinal)) {
                Stop-Install "Archive file outside $SubtreePrefix`: [$name]"
            }
            if (-not $expected.ContainsKey($clean) -or -not (@($expected.Keys) -ccontains $clean)) {
                Stop-Install "Unexpected archive file: [$name]"
            }
            if (-not $seenFiles.Add($clean)) { Stop-Install "Duplicate archive file: [$name]" }
            $rel = $clean.Substring($SubtreePrefix.Length)
            $target = [IO.Path]::GetFullPath((Join-Path $destRoot $rel.Replace('/', '\')))
            if (-not $target.StartsWith($destPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                Stop-Install "Archive path escapes the release root: [$name]"
            }
            $parent = [IO.Path]::GetDirectoryName($target)
            if ($parent) { [void][IO.Directory]::CreateDirectory($parent) }
            $input = $entry.Open()
            try {
                $output = [IO.File]::Open($target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
                try { $input.CopyTo($output) } finally { $output.Dispose() }
            }
            finally { $input.Dispose() }
        }
    }
    finally { $zip.Dispose() }
    if ($seenFiles.Count -ne $expected.Count) {
        Stop-Install "Archive file count $($seenFiles.Count) does not match committed count $($expected.Count)."
    }
    Assert-TreeMatches $destRoot $SubtreePrefix $Inventory $GitRepo 'staged release'
}

function New-Junction {
    param([string]$Path, [string]$Target, [string]$Label)
    if (Test-AnyEntry $Path) { Stop-Install "$Label already exists; refusing to overwrite: $Path" }
    New-Item -ItemType Junction -Path $Path -Target $Target -ErrorAction Stop | Out-Null
}

function Remove-JunctionOnly {
    param([string]$Path, [string]$ExpectedTarget)
    $actual = Get-JunctionTarget $Path 'junction to remove'
    if (-not (Test-SamePath $actual $ExpectedTarget)) {
        Stop-Install "Refusing to remove junction with unexpected target: $Path -> $actual"
    }
    [IO.Directory]::Delete($Path, $false)
    if (Test-AnyEntry $Path) { Stop-Install "Junction still exists after non-recursive delete: $Path" }
}

function Test-JunctionHasTarget {
    param([string]$Path, [string]$ExpectedTarget)
    if (-not (Test-AnyEntry $Path)) { return $false }
    try { $actual = Get-JunctionTarget $Path 'junction' } catch { return $false }
    return (Test-SamePath $actual $ExpectedTarget)
}

try {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        Stop-Install 'Windows junctions require Windows.'
    }
    if ($Commit -notmatch '^[0-9a-fA-F]{40}$') {
        Stop-Install 'Commit must be a full 40-character hexadecimal commit id.'
    }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Stop-Install 'git is not available on PATH.' }

    $RepoPath = Normalize-Path (Resolve-Path -LiteralPath $Repo).Path
    Assert-RealDirectory $RepoPath 'Repo'
    $topOut = @(& git -C $RepoPath rev-parse --show-toplevel 2>$null)
    $topCode = $LASTEXITCODE
    if ($topCode -ne 0) { Stop-Install "Not a Git worktree: $RepoPath" }
    $top = Normalize-Path (($topOut -join '').Trim())
    if (-not (Test-SamePath $top $RepoPath)) {
        Stop-Install "Repo must be the root of a Git worktree: $RepoPath"
    }
    $resolvedOut = @(& git -C $RepoPath rev-parse --verify "$Commit^{commit}" 2>$null)
    $resolveCode = $LASTEXITCODE
    if ($resolveCode -ne 0) { Stop-Install "Commit does not exist in this repository: $Commit" }
    $resolved = (($resolvedOut -join '').Trim()).ToLowerInvariant()
    if (-not [string]::Equals($resolved, $Commit, [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Install "Commit does not resolve to the supplied full commit id: $Commit"
    }
    $Commit = $resolved
    $inventory = Get-CommittedInventory $RepoPath $Commit

    $ReleaseRootPath = Normalize-Path $ReleaseRoot
    $ReleasePath = Normalize-Path (Join-Path $ReleaseRootPath $Commit)
    if (Test-SamePath $ReleaseRootPath $ReleasePath) { Stop-Install 'Release root and commit path must differ.' }
    if (Test-AnyEntry $ReleaseRootPath) { Assert-RealDirectory $ReleaseRootPath 'release root' }

    $releaseExists = $false
    if (Test-AnyEntry $ReleasePath) {
        Assert-RealDirectory $ReleasePath 'existing release'
        Assert-TreeMatches $ReleasePath $SubtreePrefix $inventory $RepoPath 'existing release'
        $releaseExists = $true
    }

    $resolvedProviderRoots = @()
    foreach ($root in $ProviderRoot) {
        $rp = Normalize-Path $root
        Assert-RealDirectory $rp 'provider root'
        foreach ($other in $resolvedProviderRoots) {
            if (Test-SamePath $rp $other) { Stop-Install "Duplicate provider root: $rp" }
        }
        $resolvedProviderRoots += $rp
    }

    $records = @()
    foreach ($root in $resolvedProviderRoots) {
        foreach ($id in $ExpectedSkills) {
            $link = Normalize-Path (Join-Path $root $id)
            $oldTarget = Normalize-Path (Join-Path (Join-Path (Join-Path $RepoPath '.agents') 'skills') $id)
            $newTarget = Normalize-Path (Join-Path $ReleasePath $id)
            $actual = Get-JunctionTarget $link 'installed skill link'
            $isOld = Test-SamePath $actual $oldTarget
            $isNew = Test-SamePath $actual $newTarget
            if (-not ($isOld -or $isNew)) {
                Stop-Install "Refusing mismatched link $link`: target is $actual; expected $oldTarget or $newTarget"
            }
            Assert-RealDirectory $actual 'link target'
            $skillFile = Join-Path $actual 'SKILL.md'
            if (-not [IO.File]::Exists($skillFile)) {
                Stop-Install "Link target has no SKILL.md: $link -> $actual"
            }
            if ($isNew -and -not $releaseExists) {
                Stop-Install "Link points at the requested release, but that release is not installed: $link"
            }
            $records += [pscustomobject]@{
                Link      = $link
                OldTarget = $oldTarget
                NewTarget = $newTarget
                Current   = $actual
                IsCurrent = $isNew
            }
        }
    }
    $expectedLinkCount = $resolvedProviderRoots.Count * $ExpectedSkills.Count
    if ($records.Count -ne $expectedLinkCount) {
        Stop-Install "Expected $expectedLinkCount skill links, found $($records.Count)."
    }

    $toChange = @($records | Where-Object { -not $_.IsCurrent })
    Write-Host "Repo:    $RepoPath"
    Write-Host "Commit:  $Commit"
    Write-Host "Release: $ReleasePath ($(if ($releaseExists) { 'verified existing' } else { 'will install' }))"
    Write-Host "Skills:  $($ExpectedSkills.Count) committed, $($inventory.Files.Count) files"
    foreach ($record in $records) {
        $action = if ($record.IsCurrent) { 'current' } else { 'repoint' }
        Write-Host ("  {0,-8} {1} -> {2}" -f $action, $record.Link, $record.NewTarget)
    }
    if ($toChange.Count -eq 0 -and $releaseExists) {
        Write-Host 'Already installed and verified; no changes needed.'
        exit 0
    }

    if (-not $PSCmdlet.ShouldProcess(
            "$ReleasePath and $($records.Count) provider links",
            "Install committed skills $Commit and repoint $($toChange.Count) junction(s)")) {
        Write-Host 'Dry run complete; no filesystem changes made.'
        exit 0
    }

    $stagingRelease = $null
    if (-not $releaseExists) {
        [void][IO.Directory]::CreateDirectory($ReleaseRootPath)
        Assert-RealDirectory $ReleaseRootPath 'release root'
        $stagingRelease = Join-Path $ReleaseRootPath ('.staging-' + $Commit + '-' + [guid]::NewGuid().ToString('N'))
        [void][IO.Directory]::CreateDirectory($stagingRelease)
        $archive = Join-Path ([IO.Path]::GetTempPath()) ('unity-playtest-skills-' + $Commit + '-' + [guid]::NewGuid().ToString('N') + '.zip')
        try {
            $archiveOut = @(& git -C $RepoPath -c core.autocrlf=false -c core.eol=lf archive --format=zip "--output=$archive" $Commit -- $Subtree 2>&1)
            if ($LASTEXITCODE -ne 0) { Stop-Install "git archive failed: $($archiveOut -join ' ')" }
            if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) { Stop-Install "git archive did not create $archive" }
            Expand-CommittedArchive $archive $stagingRelease $inventory $RepoPath
            if (Test-AnyEntry $ReleasePath) { Stop-Install "Release appeared during install; refusing to replace: $ReleasePath" }
            [IO.Directory]::Move($stagingRelease, $ReleasePath)
            $stagingRelease = $null
            Assert-RealDirectory $ReleasePath 'installed release'
            Assert-TreeMatches $ReleasePath $SubtreePrefix $inventory $RepoPath 'installed release'
        }
        catch {
            if ($stagingRelease -and (Test-AnyEntry $stagingRelease)) {
                Write-Warning "Install failed; staging retained for inspection: $stagingRelease"
            }
            throw
        }
    }

    $staged = @()
    $changed = @()
    try {
        foreach ($record in $toChange) {
            $stage = Normalize-Path (Join-Path (Split-Path -Parent $record.Link) ('.skills-stage-' + [guid]::NewGuid().ToString('N')))
            New-Junction $stage $record.NewTarget 'staged junction'
            $stageTarget = Get-JunctionTarget $stage 'staged junction'
            if (-not (Test-SamePath $stageTarget $record.NewTarget)) {
                Stop-Install "Staged junction target mismatch: $stage -> $stageTarget"
            }
            Assert-TreeMatches $stage ($SubtreePrefix + (Split-Path -Leaf $record.Link) + '/') $inventory $RepoPath 'staged link content'
            $staged += [pscustomobject]@{ Record = $record; Stage = $stage }
        }

        foreach ($item in $staged) {
            $record = $item.Record
            $backup = Normalize-Path (Join-Path (Split-Path -Parent $record.Link) ('.skills-backup-' + [guid]::NewGuid().ToString('N')))
            if (Test-AnyEntry $backup) { Stop-Install "Backup path already exists: $backup" }
            [IO.Directory]::Move($record.Link, $backup)
            $record | Add-Member -NotePropertyName Backup -NotePropertyValue $backup -Force
            $changed += $record
            $backupTarget = Get-JunctionTarget $backup 'backup junction'
            if (-not (Test-SamePath $backupTarget $record.OldTarget)) {
                Stop-Install "Backup junction target mismatch: $backup -> $backupTarget"
            }
            [IO.Directory]::Move($item.Stage, $record.Link)
            $finalTarget = Get-JunctionTarget $record.Link 'new installed link'
            if (-not (Test-SamePath $finalTarget $record.NewTarget)) {
                Stop-Install "New link target mismatch: $($record.Link) -> $finalTarget"
            }
            Assert-TreeMatches $record.Link ($SubtreePrefix + (Split-Path -Leaf $record.Link) + '/') $inventory $RepoPath 'new installed link content'
            if (-not (Test-SamePath (Get-JunctionTarget $backup 'backup junction') $record.OldTarget)) {
                Stop-Install "Backup junction changed before commit: $backup"
            }
        }
    }
    catch {
        $original = $_.Exception.Message
        $rollbackErrors = @()
        for ($i = $changed.Count - 1; $i -ge 0; $i--) {
            $record = $changed[$i]
            try {
                if (Test-JunctionHasTarget $record.Link $record.NewTarget) {
                    Remove-JunctionOnly $record.Link $record.NewTarget
                }
                elseif (Test-AnyEntry $record.Link) {
                    throw "New link is not the verified junction; refusing to remove: $($record.Link)"
                }
                if (Test-JunctionHasTarget $record.Backup $record.OldTarget) {
                    if (Test-AnyEntry $record.Link) { throw "Cannot restore backup; link path exists: $($record.Link)" }
                    [IO.Directory]::Move($record.Backup, $record.Link)
                }
                elseif (Test-AnyEntry $record.Backup) {
                    throw "Backup is not the verified old junction; refusing to restore: $($record.Backup)"
                }
            }
            catch { $rollbackErrors += $_.Exception.Message }
        }
        foreach ($item in $staged) {
            try {
                if (Test-JunctionHasTarget $item.Stage $item.Record.NewTarget) {
                    Remove-JunctionOnly $item.Stage $item.Record.NewTarget
                }
                elseif (Test-AnyEntry $item.Stage) {
                    $rollbackErrors += "Staged path is not the verified junction; left in place: $($item.Stage)"
                }
            }
            catch { $rollbackErrors += $_.Exception.Message }
        }
        if ($rollbackErrors.Count -gt 0) {
            Stop-Install ("$original`nRollback incomplete:`n" + ($rollbackErrors -join "`n"))
        }
        Stop-Install "$original`nRollback restored the previous junctions."
    }

    foreach ($record in $changed) {
        try {
            if (Test-JunctionHasTarget $record.Backup $record.OldTarget) {
                Remove-JunctionOnly $record.Backup $record.OldTarget
            }
            elseif (Test-AnyEntry $record.Backup) {
                Write-Warning "Backup is not the verified old junction; left in place: $($record.Backup)"
            }
        }
        catch { Write-Warning "Could not remove verified backup $($record.Backup): $($_.Exception.Message)" }
    }

    Write-Host "OK: release $Commit verified; $($changed.Count) link(s) repointed; $($records.Count - $changed.Count) already current."
    exit 0
}
catch {
    [Console]::Error.WriteLine('ERROR: ' + $_.Exception.Message)
    exit 1
}
