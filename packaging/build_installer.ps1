param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExecutable,

    [switch]$SkipApplicationBuild,

    [switch]$IndependentPreview,

    [string]$InnoCompiler,

    [string]$CertificateThumbprint = $env:VANIOR_SIGNING_CERT_THUMBPRINT,

    [string]$SignToolPath,

    [string]$TimestampUrl = "https://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"
$Repository = Split-Path -Parent $PSScriptRoot
$ApplicationExe = Join-Path $Repository "dist\VANIOR PRINT\VANIOR PRINT.exe"
$InstallerScript = Join-Path $PSScriptRoot "installer.iss"
$Version = (& $PythonExecutable -c "import pathlib,tomllib; print(tomllib.loads(pathlib.Path(r'$Repository\pyproject.toml').read_text(encoding='utf-8'))['project']['version'])").Trim()
if ($LASTEXITCODE -ne 0 -or $Version -notmatch '^\d+\.\d+(?:\.\d+)?$') {
    throw "Cannot read a semantic application version from pyproject.toml"
}
$InstallerBaseName = "VANIOR PRINT Independent Setup v$Version"
$InstallerExe = Join-Path $Repository "dist\installer\$InstallerBaseName.exe"

function Resolve-SignTool {
    if ($SignToolPath) {
        if (-not (Test-Path -LiteralPath $SignToolPath -PathType Leaf)) {
            throw "SignTool was not found: $SignToolPath"
        }
        return [IO.Path]::GetFullPath($SignToolPath)
    }
    $Candidates = Get-ChildItem `
        -LiteralPath "C:\Program Files (x86)\Windows Kits\10\bin" `
        -Filter "signtool.exe" `
        -File `
        -Recurse `
        -ErrorAction SilentlyContinue | Where-Object {
            $_.DirectoryName -match '\\x64$'
        } | Sort-Object FullName -Descending
    if (-not $Candidates) {
        throw "SignTool was not found. Install the Windows SDK signing tools."
    }
    return $Candidates[0].FullName
}

function Invoke-ReleaseSigning([string]$Path) {
    if (-not $CertificateThumbprint) {
        return
    }
    $Thumbprint = $CertificateThumbprint.Replace(" ", "").ToUpperInvariant()
    $Certificate = Get-Item -LiteralPath "Cert:\CurrentUser\My\$Thumbprint" -ErrorAction SilentlyContinue
    if (-not $Certificate -or -not $Certificate.HasPrivateKey) {
        throw "A code-signing certificate with a private key was not found in CurrentUser\\My: $Thumbprint"
    }
    $Tool = Resolve-SignTool
    & $Tool sign /sha1 $Thumbprint /fd SHA256 /tr $TimestampUrl /td SHA256 `
        /d "VANIOR PRINT" /du "https://vanior-print.jonni2k25.chatgpt.site/" $Path
    if ($LASTEXITCODE -ne 0) {
        throw "Authenticode signing failed for $Path"
    }
    $Signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($Signature.Status -ne "Valid") {
        throw "Authenticode verification failed for ${Path}: $($Signature.Status)"
    }
}

if (-not $SkipApplicationBuild) {
    # ``build_windows.ps1`` packages directly from the source checkout and
    # keeps the version from ``version.py``. Avoid modifying the caller's
    # virtual environment here: a locked editable install must never prevent
    # a user from producing a release installer.
    & (Join-Path $PSScriptRoot "build_windows.ps1") `
        -PythonExecutable $PythonExecutable `
        -IndependentPreview
    if ($LASTEXITCODE -ne 0) {
        throw "Application build failed with exit code $LASTEXITCODE"
    }
}

if (-not (Test-Path -LiteralPath $ApplicationExe -PathType Leaf)) {
    throw "Application build was not found: $ApplicationExe"
}

# When a trusted certificate is supplied, sign the application before it is
# embedded in the installer. The same identity is then used for the installer.
Invoke-ReleaseSigning $ApplicationExe

if (-not $InnoCompiler) {
    $Candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
    )
    $InnoCompiler = $Candidates | Where-Object {
        Test-Path -LiteralPath $_ -PathType Leaf
    } | Select-Object -First 1
}

if (-not $InnoCompiler) {
    throw "Inno Setup 6 compiler was not found. Install JRSoftware.InnoSetup."
}

$CompilerArguments = @("/DMyAppVersion=$Version", "/DIndependentPreview=1")
$CompilerArguments += $InstallerScript
& $InnoCompiler $CompilerArguments
if ($LASTEXITCODE -ne 0) {
    throw "Installer build failed with exit code $LASTEXITCODE"
}

if (-not (Test-Path -LiteralPath $InstallerExe -PathType Leaf)) {
    throw "Installer output was not created: $InstallerExe"
}

Invoke-ReleaseSigning $InstallerExe

$Installer = Get-Item -LiteralPath $InstallerExe
$Hash = (Get-FileHash -LiteralPath $InstallerExe -Algorithm SHA256).Hash
$ApplicationHash = (Get-FileHash -LiteralPath $ApplicationExe -Algorithm SHA256).Hash
$InstallerSignature = Get-AuthenticodeSignature -LiteralPath $InstallerExe
$ApplicationSignature = Get-AuthenticodeSignature -LiteralPath $ApplicationExe
$ChecksumPath = "$InstallerExe.sha256"
[IO.File]::WriteAllText(
    $ChecksumPath,
    "$Hash  $($Installer.Name)`r`n",
    [Text.UTF8Encoding]::new($false)
)
$ReleaseManifestPath = Join-Path $Installer.DirectoryName "VANIOR PRINT v$Version release.json"
$ReleaseManifest = [ordered]@{
    schema = "vanior-windows-release-v1"
    application = "VANIOR PRINT"
    version = $Version
    created_utc = [DateTimeOffset]::UtcNow.ToString("o")
    publisher = "Ivan Valevich"
    website = "https://vanior-print.jonni2k25.chatgpt.site/"
    packaging = [ordered]@{
        format = "PyInstaller onedir + Inno Setup"
        independent_engine = $true
        embedded_bambu_studio = $false
        direct_printer_transport = "local FTPS + MQTT/TLS"
        requests_administrator = $false
        upx = $false
        solid_compression = $false
    }
    files = @(
        [ordered]@{
            name = "VANIOR PRINT.exe"
            sha256 = $ApplicationHash
            authenticode = [string]$ApplicationSignature.Status
        },
        [ordered]@{
            name = $Installer.Name
            sha256 = $Hash
            authenticode = [string]$InstallerSignature.Status
        }
    )
}
[IO.File]::WriteAllText(
    $ReleaseManifestPath,
    ($ReleaseManifest | ConvertTo-Json -Depth 6),
    [Text.UTF8Encoding]::new($false)
)
Write-Host "Installer: $($Installer.FullName)"
Write-Host "Size: $($Installer.Length) bytes"
Write-Host "SHA256: $Hash"
Write-Host "Authenticode: $($InstallerSignature.Status)"
Write-Host "Checksum: $ChecksumPath"
Write-Host "Release manifest: $ReleaseManifestPath"
