param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExecutable,

    [switch]$IndependentPreview
)

$ErrorActionPreference = "Stop"
$Repository = Split-Path -Parent $PSScriptRoot
$Source = Join-Path $Repository "src"
$Entry = Join-Path $PSScriptRoot "gui_entry.py"
$Icon = Join-Path $Source "ai_print_optimizer\assets\vanior_print.ico"
$Manifest = Join-Path $PSScriptRoot "app.manifest"
$VersionTemplate = Join-Path $PSScriptRoot "version_info.template.txt"
$VersionSource = Join-Path $Source "ai_print_optimizer\version.py"
$VersionText = Get-Content -LiteralPath $VersionSource -Raw
if ($VersionText -notmatch '__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"') {
    throw "Cannot read the application version from $VersionSource"
}
$Version = "$($Matches[1]).$($Matches[2]).$($Matches[3])"
$VersionTuple = "$($Matches[1]), $($Matches[2]), $($Matches[3]), 0"
$BuildDirectory = Join-Path $Repository "build"
[IO.Directory]::CreateDirectory($BuildDirectory) | Out-Null
$VersionResource = Join-Path $BuildDirectory "vanior_print_version_info.txt"
$VersionResourceText = (Get-Content -LiteralPath $VersionTemplate -Raw).
    Replace("__VERSION__", $Version).
    Replace("__VERSION_TUPLE__", $VersionTuple)
[IO.File]::WriteAllText(
    $VersionResource,
    $VersionResourceText,
    [Text.UTF8Encoding]::new($false)
)

# Do not install the source tree into the selected Python environment here.
#
# A developer can have an older editable install which is locked by a running
# process. More importantly, packaging must not mutate their working virtual
# environment. The application version is bundled from
# ``ai_print_optimizer/version.py`` and frozen diagnostics deliberately treat
# that runtime value as authoritative, so PyInstaller only needs the source
# path below.

& $PythonExecutable -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --onedir `
    --noupx `
    --name "VANIOR PRINT" `
    --icon $Icon `
    --manifest $Manifest `
    --version-file $VersionResource `
    --paths $Source `
    --collect-data ai_print_optimizer `
    --distpath (Join-Path $Repository "dist") `
    --workpath (Join-Path $Repository "build\pyinstaller") `
    --specpath (Join-Path $Repository "build") `
    $Entry

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE"
}

# Some development environments expose Poppler's ICU DLLs through PATH.
# PyInstaller may then copy them into the application root, where the foreign
# ICU runtime shadows Qt's own dependencies and makes PySide6.QtCore fail to
# load. VANIOR PRINT does not use Poppler, so remove only these known root-level
# collisions from the freshly generated bundle.
$Internal = Join-Path $Repository "dist\VANIOR PRINT\_internal"
$ConflictingIcu = @(
    (Join-Path $Internal "icuuc.dll")
) + @(Get-ChildItem -LiteralPath $Internal -Filter "icudt*.dll" -File -ErrorAction SilentlyContinue | ForEach-Object FullName)
foreach ($Conflict in $ConflictingIcu | Select-Object -Unique) {
    if (Test-Path -LiteralPath $Conflict) {
        Remove-Item -LiteralPath $Conflict -Force
    }
}

# An editable development environment may contain metadata from an older
# VANIOR PRINT build.  Runtime code comes from the checkout above, so retaining
# that foreign dist-info would make diagnostics report two different versions.
Get-ChildItem -LiteralPath $Internal -Directory -Filter "ai_print_optimizer-*.dist-info" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force

$ApplicationDirectory = Join-Path $Repository "dist\VANIOR PRINT"
$IndependentMarker = Join-Path $ApplicationDirectory "independent-engine"
[IO.File]::WriteAllText(
    $IndependentMarker,
    "VANIOR Slice independent engine`r`n",
    [Text.UTF8Encoding]::new($false)
)

& (Join-Path $PSScriptRoot "stage_legal_notices.ps1") `
    -ApplicationDirectory $ApplicationDirectory `
    -PythonExecutable $PythonExecutable
if ($LASTEXITCODE -ne 0) {
    throw "Legal notice staging failed with exit code $LASTEXITCODE"
}
