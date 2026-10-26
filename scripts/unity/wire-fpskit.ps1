<#
.SYNOPSIS
Points a Unity project's package manifest at this repository's bridge package.

.DESCRIPTION
Adds "com.playtest.bridge": "file:<absolute package path>" to
Packages/manifest.json and lists the package in testables so its EditMode
tests are compiled and discovered. The rest of the manifest is preserved
semantically; existing dependencies keep their values.

With -Remove the dependency and the testables entry are removed again, which
restores the project.

This script never starts Unity and never copies the package into Assets/.
Unity resolves the file: dependency the next time the project opens.

.PARAMETER ProjectPath
Unity project root to wire. Pass it explicitly or set FPSKIT_BRIDGE_PROJECT.
There is no personal-path default.

.PARAMETER PackagePath
The com.playtest.bridge package directory. Defaults to bridge/ in this
repository.

.PARAMETER Remove
Remove the wiring instead of adding it.

.EXAMPLE
powershell -File scripts/unity/wire-fpskit.ps1 -ProjectPath "C:\path\to\FPSKit-bridge-v0"

.EXAMPLE
powershell -File scripts/unity/wire-fpskit.ps1 -ProjectPath "C:\path\to\FPSKit-bridge-v0" -Remove
#>
[CmdletBinding()]
param(
    [string]$ProjectPath = $env:FPSKIT_BRIDGE_PROJECT,

    [string]$PackagePath,

    [switch]$Remove
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$PackageName = 'com.playtest.bridge'

function ConvertTo-JsonStringLiteral {
    param([string]$Text)
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append('"')
    foreach ($character in $Text.ToCharArray()) {
        $code = [int]$character
        if ($code -eq 0x22) { [void]$builder.Append('\"') }
        elseif ($code -eq 0x5C) { [void]$builder.Append('\\') }
        elseif ($code -eq 0x0A) { [void]$builder.Append('\n') }
        elseif ($code -eq 0x0D) { [void]$builder.Append('\r') }
        elseif ($code -eq 0x09) { [void]$builder.Append('\t') }
        elseif ($code -lt 0x20) { [void]$builder.Append(('\u{0:x4}' -f $code)) }
        else { [void]$builder.Append($character) }
    }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function ConvertTo-ManifestJson {
    param($Value, [int]$Indent = 0)
    $pad = ' ' * $Indent
    $inner = ' ' * ($Indent + 2)
    if ($null -eq $Value) { return 'null' }
    if ($Value -is [string]) { return (ConvertTo-JsonStringLiteral $Value) }
    if ($Value -is [bool]) {
        if ($Value) { return 'true' }
        return 'false'
    }
    if ($Value -is [int] -or $Value -is [long] -or $Value -is [double] -or $Value -is [decimal]) {
        return [string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0}', $Value)
    }
    if ($Value -is [System.Collections.IDictionary]) {
        $keys = @($Value.Keys)
        if ($keys.Count -eq 0) { return '{}' }
        $parts = @()
        foreach ($key in $keys) {
            $parts += ($inner + (ConvertTo-JsonStringLiteral ([string]$key)) + ': ' + (ConvertTo-ManifestJson $Value[$key] ($Indent + 2)))
        }
        return "{`n" + ($parts -join ",`n") + "`n" + $pad + '}'
    }
    if ($Value -is [System.Collections.IList]) {
        $items = @($Value)
        if ($items.Count -eq 0) { return '[]' }
        $parts = @()
        foreach ($item in $items) {
            $parts += ($inner + (ConvertTo-ManifestJson $item ($Indent + 2)))
        }
        return "[`n" + ($parts -join ",`n") + "`n" + $pad + ']'
    }
    $properties = @($Value.PSObject.Properties)
    if ($properties.Count -eq 0) { return '{}' }
    $parts = @()
    foreach ($property in $properties) {
        $parts += ($inner + (ConvertTo-JsonStringLiteral $property.Name) + ': ' + (ConvertTo-ManifestJson $property.Value ($Indent + 2)))
    }
    return "{`n" + ($parts -join ",`n") + "`n" + $pad + '}'
}

function Get-ManifestArrayText {
    param($Manifest, [string]$Name)
    $property = $Manifest.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value) { return $null }
    return (@($property.Value | ForEach-Object { [string]$_ }) -join ', ')
}

try {
    if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
        throw 'ProjectPath is required. Pass -ProjectPath or set FPSKIT_BRIDGE_PROJECT.'
    }
    if (-not (Test-Path -LiteralPath $ProjectPath -PathType Container)) {
        throw "Unity project not found: $ProjectPath"
    }
    $projectFull = (Resolve-Path -LiteralPath $ProjectPath).ProviderPath
    $manifestPath = Join-Path $projectFull 'Packages\manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Not a Unity project (no Packages/manifest.json): $projectFull"
    }

    if ([string]::IsNullOrWhiteSpace($PackagePath)) {
        $PackagePath = Join-Path (Join-Path $PSScriptRoot '..\..') 'bridge'
    }
    if (-not (Test-Path -LiteralPath $PackagePath -PathType Container)) {
        throw "Package directory not found: $PackagePath"
    }
    $packageFull = (Resolve-Path -LiteralPath $PackagePath).ProviderPath
    $packageManifestPath = Join-Path $packageFull 'package.json'
    if (-not (Test-Path -LiteralPath $packageManifestPath -PathType Leaf)) {
        throw "No package.json in the package directory: $packageFull"
    }
    try { $packageManifest = [IO.File]::ReadAllText($packageManifestPath) | ConvertFrom-Json }
    catch { throw "package.json is not valid JSON: $packageManifestPath ($($_.Exception.Message))" }
    $declaredName = $null
    if ($null -ne $packageManifest) {
        $nameProperty = $packageManifest.PSObject.Properties['name']
        if ($null -ne $nameProperty) { $declaredName = [string]$nameProperty.Value }
    }
    if (-not [string]::Equals($declaredName, $PackageName, [StringComparison]::Ordinal)) {
        throw "package.json does not declare $PackageName (found '$declaredName'): $packageManifestPath"
    }

    $dependencyValue = 'file:' + $packageFull.Replace('\', '/')

    $manifestText = [IO.File]::ReadAllText($manifestPath)
    $newline = "`n"
    if ($manifestText.Contains("`r`n")) { $newline = "`r`n" }
    try { $manifest = $manifestText | ConvertFrom-Json }
    catch { throw "Packages/manifest.json is not valid JSON: $($_.Exception.Message)" }
    if ($null -eq $manifest -or $manifest -isnot [System.Management.Automation.PSCustomObject]) {
        throw "Packages/manifest.json must contain a JSON object: $manifestPath"
    }

    $dependenciesProperty = $manifest.PSObject.Properties['dependencies']
    $dependencies = $null
    if ($null -ne $dependenciesProperty) { $dependencies = $dependenciesProperty.Value }
    if ($null -eq $dependencies) {
        $dependencies = [pscustomobject]@{}
        Add-Member -InputObject $manifest -NotePropertyName 'dependencies' -NotePropertyValue $dependencies -Force
    }
    if ($dependencies -isnot [System.Management.Automation.PSCustomObject]) {
        throw "Packages/manifest.json has a dependencies value that is not an object: $manifestPath"
    }

    $beforeDependency = $null
    $dependencyEntry = $dependencies.PSObject.Properties[$PackageName]
    if ($null -ne $dependencyEntry) { $beforeDependency = [string]$dependencyEntry.Value }
    $beforeTestables = Get-ManifestArrayText $manifest 'testables'

    $testablesProperty = $manifest.PSObject.Properties['testables']
    $testables = @()
    if ($null -ne $testablesProperty -and $null -ne $testablesProperty.Value) {
        if ($testablesProperty.Value -isnot [System.Collections.IList]) {
            throw "Packages/manifest.json has a testables value that is not an array: $manifestPath"
        }
        $testables = @($testablesProperty.Value | ForEach-Object { [string]$_ })
    }

    if ($Remove) {
        if ($null -ne $dependencies.PSObject.Properties[$PackageName]) {
            $dependencies.PSObject.Properties.Remove($PackageName)
        }
        $testables = @($testables | Where-Object { -not [string]::Equals($_, $PackageName, [StringComparison]::Ordinal) })
    }
    else {
        Add-Member -InputObject $dependencies -NotePropertyName $PackageName -NotePropertyValue $dependencyValue -Force
        if (-not ($testables -contains $PackageName)) { $testables = @($testables + $PackageName) }
    }

    if ($testables.Count -eq 0) {
        if ($null -ne $manifest.PSObject.Properties['testables']) {
            $manifest.PSObject.Properties.Remove('testables')
        }
    }
    else {
        $testableArray = [string[]]@($testables)
        if ($null -ne $manifest.PSObject.Properties['testables']) {
            $manifest.testables = $testableArray
        }
        else {
            Add-Member -InputObject $manifest -NotePropertyName 'testables' -NotePropertyValue $testableArray -Force
        }
    }

    $afterDependency = $null
    $afterEntry = $manifest.dependencies.PSObject.Properties[$PackageName]
    if ($null -ne $afterEntry) { $afterDependency = [string]$afterEntry.Value }
    $afterTestables = Get-ManifestArrayText $manifest 'testables'

    $changed = -not [string]::Equals($beforeDependency, $afterDependency, [StringComparison]::Ordinal) -or
        -not [string]::Equals($beforeTestables, $afterTestables, [StringComparison]::Ordinal)

    $beforeDependencyText = '(absent)'
    if ($null -ne $beforeDependency) { $beforeDependencyText = $beforeDependency }
    $afterDependencyText = '(absent)'
    if ($null -ne $afterDependency) { $afterDependencyText = $afterDependency }
    $beforeTestablesText = '(absent)'
    if ($null -ne $beforeTestables -and $beforeTestables.Length -gt 0) { $beforeTestablesText = $beforeTestables }
    elseif ($null -ne $beforeTestables) { $beforeTestablesText = '(empty)' }
    $afterTestablesText = '(absent)'
    if ($null -ne $afterTestables -and $afterTestables.Length -gt 0) { $afterTestablesText = $afterTestables }
    elseif ($null -ne $afterTestables) { $afterTestablesText = '(empty)' }

    Write-Host "Project:   $projectFull"
    Write-Host "Manifest:  $manifestPath"
    Write-Host "Package:   $packageFull"
    Write-Host "Before:    dependencies[$PackageName] = $beforeDependencyText"
    Write-Host "After:     dependencies[$PackageName] = $afterDependencyText"
    Write-Host "Testables before: $beforeTestablesText"
    Write-Host "Testables after:  $afterTestablesText"

    if (-not $changed) {
        Write-Host 'Manifest already in the requested state; nothing written.'
        exit 0
    }

    $jsonText = (ConvertTo-ManifestJson $manifest 0)
    if ($newline -eq "`r`n") { $jsonText = $jsonText.Replace("`n", "`r`n") }
    $jsonText = $jsonText + $newline

    try { $roundTrip = $jsonText | ConvertFrom-Json }
    catch { throw "Refusing to write: the serialized manifest does not parse ($($_.Exception.Message))" }
    if ($null -eq $roundTrip -or $roundTrip -isnot [System.Management.Automation.PSCustomObject]) {
        throw 'Refusing to write: the serialized manifest is not a JSON object.'
    }

    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($manifestPath, $jsonText, $utf8)

    try { $written = [IO.File]::ReadAllText($manifestPath) | ConvertFrom-Json }
    catch { throw "The written manifest does not parse: $manifestPath ($($_.Exception.Message))" }
    $writtenDependency = $null
    $writtenEntry = $written.PSObject.Properties['dependencies']
    if ($null -ne $writtenEntry -and $null -ne $writtenEntry.Value) {
        $writtenKey = $writtenEntry.Value.PSObject.Properties[$PackageName]
        if ($null -ne $writtenKey) { $writtenDependency = [string]$writtenKey.Value }
    }
    if (-not [string]::Equals($writtenDependency, $afterDependency, [StringComparison]::Ordinal)) {
        throw "The written manifest reads back as dependencies[$PackageName] = '$writtenDependency', expected '$afterDependency'."
    }

    Write-Host 'Wrote the manifest.'
    $verifiedText = '(absent)'
    if (-not [string]::IsNullOrEmpty($writtenDependency)) { $verifiedText = $writtenDependency }
    Write-Host "Verified:  dependencies[$PackageName] = $verifiedText"
    exit 0
}
catch {
    [Console]::Error.WriteLine('ERROR: ' + $_.Exception.Message)
    exit 1
}
