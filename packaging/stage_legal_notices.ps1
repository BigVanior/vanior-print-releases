param(
    [Parameter(Mandatory = $true)]
    [string]$ApplicationDirectory,

    [Parameter(Mandatory = $true)]
    [string]$PythonExecutable
)

$ErrorActionPreference = "Stop"
$Repository = Split-Path -Parent $PSScriptRoot
$Application = (Resolve-Path -LiteralPath $ApplicationDirectory).Path
$Legal = Join-Path $Application "_internal\legal"
[IO.Directory]::CreateDirectory($Legal) | Out-Null

Copy-Item -LiteralPath (Join-Path $Repository "LICENSE") -Destination (Join-Path $Legal "VANIOR_PRINT_LICENSE.txt") -Force
Copy-Item -LiteralPath (Join-Path $Repository "docs\PRIVACY_POLICY_RU.md") -Destination (Join-Path $Legal "PRIVACY_POLICY_RU.txt") -Force
Copy-Item -LiteralPath (Join-Path $Repository "docs\TERMS_OF_USE_RU.md") -Destination (Join-Path $Legal "TERMS_OF_USE_RU.txt") -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "THIRD_PARTY_NOTICES_RU.txt") -Destination (Join-Path $Legal "THIRD_PARTY_NOTICES_RU.txt") -Force
Copy-Item -LiteralPath (Join-Path $Repository "requirements-lock.txt") -Destination (Join-Path $Legal "DEPENDENCY_VERSIONS.txt") -Force

$LgplUrl = "https://www.gnu.org/licenses/lgpl-3.0.txt"
$LgplHash = "E3A994D82E644B03A792A930F574002658412F62407F5FEE083F2555C5F23118"
$LgplCache = Join-Path $Repository "build\LGPL-3.0.txt"
if (
    -not (Test-Path -LiteralPath $LgplCache -PathType Leaf) -or
    (Get-FileHash -LiteralPath $LgplCache -Algorithm SHA256).Hash -ne $LgplHash
) {
    [IO.Directory]::CreateDirectory((Split-Path -Parent $LgplCache)) | Out-Null
    Invoke-WebRequest -Uri $LgplUrl -OutFile $LgplCache
}
if ((Get-FileHash -LiteralPath $LgplCache -Algorithm SHA256).Hash -ne $LgplHash) {
    throw "Downloaded LGPL-3.0 license failed SHA-256 verification."
}
Copy-Item -LiteralPath $LgplCache -Destination (Join-Path $Legal "LGPL-3.0.txt") -Force

$InventoryScript = Join-Path $PSScriptRoot "inventory_licenses.py"
$InventoryJson = (& $PythonExecutable $InventoryScript)
if ($LASTEXITCODE -ne 0) {
    throw "Cannot inventory runtime dependency licenses."
}
$Inventory = $InventoryJson | ConvertFrom-Json
foreach ($Package in $Inventory) {
    $PackageDirectory = Join-Path $Legal ("packages\" + (($Package.name + "-" + $Package.version) -replace '[^A-Za-z0-9._-]', '_'))
    [IO.Directory]::CreateDirectory($PackageDirectory) | Out-Null
    foreach ($LicensePath in $Package.license_files) {
        $DestinationName = [IO.Path]::GetFileName([string]$LicensePath)
        Copy-Item -LiteralPath ([string]$LicensePath) -Destination (Join-Path $PackageDirectory $DestinationName) -Force
    }
}
[IO.File]::WriteAllText(
    (Join-Path $Legal "DEPENDENCIES.json"),
    ($Inventory | Select-Object name, version, license | ConvertTo-Json -Depth 4),
    [Text.UTF8Encoding]::new($false)
)

Write-Host "Legal notices staged: $Legal"
