param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExecutable,

    [switch]$SkipApplicationBuild,

    [switch]$StoreSubmission,

    [string]$IdentityName = $env:VANIOR_STORE_IDENTITY_NAME,

    [string]$Publisher = $env:VANIOR_STORE_PUBLISHER,

    [string]$PublisherDisplayName = "Ivan Valevich",

    [string]$MakeAppxPath
)

$ErrorActionPreference = "Stop"
$Repository = Split-Path -Parent $PSScriptRoot
$ProjectFile = Join-Path $Repository "pyproject.toml"
$ApplicationSource = Join-Path $Repository "dist\VANIOR PRINT"
$Template = Join-Path $PSScriptRoot "AppxManifest.template.xml"
$BuildRoot = Join-Path $Repository "build\msix"
$Layout = Join-Path $BuildRoot "layout"
$Verification = Join-Path $BuildRoot "verification"
$ApplicationTarget = Join-Path $Layout "Application"
$Assets = Join-Path $Layout "Assets"
$OutputDirectory = Join-Path $Repository "dist\store"

$Version = (& $PythonExecutable -c "import pathlib,tomllib; print(tomllib.loads(pathlib.Path(r'$ProjectFile').read_text(encoding='utf-8'))['project']['version'])").Trim()
if ($LASTEXITCODE -ne 0 -or $Version -notmatch '^(\d+)\.(\d+)\.(\d+)$') {
    throw "Cannot read a three-part semantic application version."
}
$PackageVersion = "$($Matches[1]).$($Matches[2]).$($Matches[3]).0"

$UsesPlaceholderIdentity = -not $IdentityName -or -not $Publisher
if ($StoreSubmission -and $UsesPlaceholderIdentity) {
    throw "Store submission requires VANIOR_STORE_IDENTITY_NAME and VANIOR_STORE_PUBLISHER from Partner Center."
}
if (-not $IdentityName) { $IdentityName = "VaniorPrint.PublicBeta.Dev" }
if (-not $Publisher) { $Publisher = "CN=Ivan Valevich" }
if ($IdentityName -notmatch '^[A-Za-z0-9.-]{3,50}$') {
    throw "Invalid MSIX identity name: $IdentityName"
}
if ($Publisher -notmatch '^CN=') {
    throw "MSIX publisher must be a distinguished name beginning with CN=."
}

if (-not $SkipApplicationBuild) {
    & (Join-Path $PSScriptRoot "build_windows.ps1") -PythonExecutable $PythonExecutable
    if ($LASTEXITCODE -ne 0) {
        throw "Application build failed with exit code $LASTEXITCODE"
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $ApplicationSource "VANIOR PRINT.exe") -PathType Leaf)) {
    throw "Application bundle was not found: $ApplicationSource"
}

if (-not $MakeAppxPath) {
    $Installed = Get-ChildItem -LiteralPath "C:\Program Files (x86)\Windows Kits\10\bin" -Filter "makeappx.exe" -File -Recurse -ErrorAction SilentlyContinue |
        Where-Object DirectoryName -Match '\\x64$' |
        Sort-Object FullName -Descending |
        Select-Object -First 1
    if ($Installed) {
        $MakeAppxPath = $Installed.FullName
    } else {
        $MakeAppxPath = (& (Join-Path $PSScriptRoot "fetch_msix_tools.ps1") | Select-Object -Last 1)
    }
}
if (-not (Test-Path -LiteralPath $MakeAppxPath -PathType Leaf)) {
    throw "MakeAppx was not found: $MakeAppxPath"
}

$ResolvedBuild = [IO.Path]::GetFullPath($BuildRoot)
$ExpectedBuildParent = [IO.Path]::GetFullPath((Join-Path $Repository "build"))
if (-not $ResolvedBuild.StartsWith($ExpectedBuildParent + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe MSIX build directory: $ResolvedBuild"
}
if (Test-Path -LiteralPath $Layout) {
    Remove-Item -LiteralPath $Layout -Recurse -Force
}
[IO.Directory]::CreateDirectory($ApplicationTarget) | Out-Null
[IO.Directory]::CreateDirectory($Assets) | Out-Null
[IO.Directory]::CreateDirectory($OutputDirectory) | Out-Null
Copy-Item -Path (Join-Path $ApplicationSource "*") -Destination $ApplicationTarget -Recurse -Force

Add-Type -AssemblyName System.Drawing
$SourceIcon = Join-Path $Repository "src\ai_print_optimizer\assets\vanior_print_icon.png"
function Write-Tile([string]$Path, [int]$Width, [int]$Height, [int]$Inset) {
    $Source = [Drawing.Image]::FromFile($SourceIcon)
    try {
        $Bitmap = [Drawing.Bitmap]::new($Width, $Height, [Drawing.Imaging.PixelFormat]::Format32bppArgb)
        try {
            $Graphics = [Drawing.Graphics]::FromImage($Bitmap)
            try {
                $Graphics.Clear([Drawing.Color]::Transparent)
                $Graphics.CompositingQuality = [Drawing.Drawing2D.CompositingQuality]::HighQuality
                $Graphics.InterpolationMode = [Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
                $Graphics.SmoothingMode = [Drawing.Drawing2D.SmoothingMode]::HighQuality
                $Side = [Math]::Max(1, [Math]::Min($Width, $Height) - (2 * $Inset))
                $X = [int](($Width - $Side) / 2)
                $Y = [int](($Height - $Side) / 2)
                $Graphics.DrawImage($Source, $X, $Y, $Side, $Side)
            } finally {
                $Graphics.Dispose()
            }
            $Bitmap.Save($Path, [Drawing.Imaging.ImageFormat]::Png)
        } finally {
            $Bitmap.Dispose()
        }
    } finally {
        $Source.Dispose()
    }
}
Write-Tile (Join-Path $Assets "StoreLogo.png") 50 50 2
Write-Tile (Join-Path $Assets "Square44x44Logo.png") 44 44 2
Write-Tile (Join-Path $Assets "Square150x150Logo.png") 150 150 8
Write-Tile (Join-Path $Assets "Wide310x150Logo.png") 310 150 8
Write-Tile (Join-Path $Assets "Square310x310Logo.png") 310 310 16

function Escape-Xml([string]$Value) {
    return [Security.SecurityElement]::Escape($Value)
}
$Manifest = (Get-Content -LiteralPath $Template -Raw).
    Replace("__IDENTITY_NAME__", (Escape-Xml $IdentityName)).
    Replace("__PUBLISHER__", (Escape-Xml $Publisher)).
    Replace("__PUBLISHER_DISPLAY_NAME__", (Escape-Xml $PublisherDisplayName)).
    Replace("__VERSION__", $PackageVersion)
[IO.File]::WriteAllText(
    (Join-Path $Layout "AppxManifest.xml"),
    $Manifest,
    [Text.UTF8Encoding]::new($false)
)

$Output = Join-Path $OutputDirectory "VANIOR PRINT v$Version.msix"
& $MakeAppxPath pack /d $Layout /p $Output /o
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Output -PathType Leaf)) {
    throw "MakeAppx failed to create the MSIX package."
}
if (Test-Path -LiteralPath $Verification) {
    Remove-Item -LiteralPath $Verification -Recurse -Force
}
& $MakeAppxPath unpack /p $Output /d $Verification /o
if (
    $LASTEXITCODE -ne 0 -or
    -not (Test-Path -LiteralPath (Join-Path $Verification "AppxManifest.xml") -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $Verification "Application\VANIOR PRINT.exe") -PathType Leaf)
) {
    throw "MSIX structural verification failed."
}

$Hash = (Get-FileHash -LiteralPath $Output -Algorithm SHA256).Hash
[IO.File]::WriteAllText(
    "$Output.sha256",
    "$Hash  $([IO.Path]::GetFileName($Output))`r`n",
    [Text.UTF8Encoding]::new($false)
)
$Release = [ordered]@{
    schema = "vanior-store-release-v1"
    application = "VANIOR PRINT"
    version = $Version
    package_version = $PackageVersion
    created_utc = [DateTimeOffset]::UtcNow.ToString("o")
    format = "MSIX"
    architecture = "x64"
    identity_name = $IdentityName
    publisher = $Publisher
    store_identity_placeholder = $UsesPlaceholderIdentity
    microsoft_store_submission_ready = -not $UsesPlaceholderIdentity
    sha256 = $Hash
}
[IO.File]::WriteAllText(
    (Join-Path $OutputDirectory "VANIOR PRINT v$Version store-release.json"),
    ($Release | ConvertTo-Json -Depth 5),
    [Text.UTF8Encoding]::new($false)
)
Write-Host "MSIX: $Output"
Write-Host "SHA256: $Hash"
if ($UsesPlaceholderIdentity) {
    Write-Warning "Development identity used. Rebuild with Partner Center identity before Store submission."
}
