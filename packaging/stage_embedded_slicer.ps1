param(
    [Parameter(Mandatory = $true)]
    [string]$ApplicationDirectory,

    [string]$SlicerSource = "C:\Program Files\Bambu Studio"
)

$ErrorActionPreference = "Stop"
$Repository = Split-Path -Parent $PSScriptRoot
$Application = (Resolve-Path -LiteralPath $ApplicationDirectory).Path
$Source = (Resolve-Path -LiteralPath $SlicerSource).Path
$Destination = Join-Path $Application "_internal\slicer"
$ExpectedVersion = "02.08.02.61"
$LicenseUrl = "https://raw.githubusercontent.com/bambulab/BambuStudio/v02.08.02.61/LICENSE"
$ExpectedLicenseHash = "57C8FF33C9C0CFC3EF00E650A1CC910D7EE479A8BC509F6C9209A7C2A11399D6"
$LicenseCache = Join-Path $Repository "build\BambuStudio-AGPL-3.0-v02.08.02.61.txt"

$Executable = Join-Path $Source "bambu-studio.exe"
$Library = Join-Path $Source "BambuStudio.dll"
$Profiles = Join-Path $Source "resources\profiles\BBL"
foreach ($Required in @($Executable, $Library, $Profiles)) {
    if (-not (Test-Path -LiteralPath $Required)) {
        throw "Required embedded slicer component was not found: $Required"
    }
}
$DetectedVersion = (Get-Item -LiteralPath $Executable).VersionInfo.FileVersion
if ($DetectedVersion -ne $ExpectedVersion) {
    throw "Embedded slicer version $DetectedVersion does not match $ExpectedVersion"
}
if (Test-Path -LiteralPath $Destination) {
    throw "Embedded slicer destination already exists: $Destination"
}

New-Item -ItemType Directory -Path $Destination | Out-Null
Get-ChildItem -LiteralPath $Source -File |
    Where-Object Name -NotIn @("Uninstall.exe") |
    Copy-Item -Destination $Destination

$ProfileDestination = Join-Path $Destination "resources\profiles"
New-Item -ItemType Directory -Path $ProfileDestination -Force | Out-Null
Copy-Item -LiteralPath $Profiles -Destination (Join-Path $ProfileDestination "BBL") -Recurse
foreach ($ProfileFile in @("BBL.json", "blacklist.json")) {
    Copy-Item -LiteralPath (Join-Path $Source "resources\profiles\$ProfileFile") -Destination $ProfileDestination
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "THIRD_PARTY_NOTICES_RU.txt") -Destination $Destination

if (
    -not (Test-Path -LiteralPath $LicenseCache) -or
    (Get-FileHash -LiteralPath $LicenseCache -Algorithm SHA256).Hash -ne $ExpectedLicenseHash
) {
    Invoke-WebRequest -Uri $LicenseUrl -OutFile $LicenseCache
}
if ((Get-FileHash -LiteralPath $LicenseCache -Algorithm SHA256).Hash -ne $ExpectedLicenseHash) {
    throw "Downloaded Bambu Studio license hash does not match the pinned release"
}
Copy-Item -LiteralPath $LicenseCache -Destination $Destination

$StagedExecutable = Join-Path $Destination "bambu-studio.exe"
if ((Get-Item -LiteralPath $StagedExecutable).VersionInfo.FileVersion -ne $ExpectedVersion) {
    throw "Staged slicing engine failed version verification"
}
$Files = Get-ChildItem -LiteralPath $Destination -Recurse -File
Write-Host "Embedded slicer: $ExpectedVersion; files: $($Files.Count); bytes: $(($Files | Measure-Object Length -Sum).Sum)"
