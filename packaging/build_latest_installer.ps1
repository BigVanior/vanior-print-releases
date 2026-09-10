param(
    [string]$Repository,
    [string]$OutputDirectory
)

$ErrorActionPreference = "Stop"

if (-not $Repository) {
    $Pointer = Join-Path $PSScriptRoot "source-path.txt"
    if (Test-Path -LiteralPath $Pointer -PathType Leaf) {
        $Repository = (Get-Content -LiteralPath $Pointer -Raw).Trim()
    } else {
        $Repository = Split-Path -Parent $PSScriptRoot
    }
}
$Repository = [IO.Path]::GetFullPath($Repository)
$ProjectFile = Join-Path $Repository "pyproject.toml"
if (-not (Test-Path -LiteralPath $ProjectFile -PathType Leaf)) {
    throw "VANIOR PRINT source was not found: $Repository"
}

$Python = Join-Path $Repository ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Project Python was not found: $Python"
}

$Version = (& $Python -c "import pathlib,tomllib; print(tomllib.loads(pathlib.Path(r'$ProjectFile').read_text(encoding='utf-8'))['project']['version'])").Trim()
if ($LASTEXITCODE -ne 0 -or $Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Cannot read the current application version"
}

& (Join-Path $Repository "packaging\build_installer.ps1") -PythonExecutable $Python
if ($LASTEXITCODE -ne 0) {
    throw "Installer build failed with exit code $LASTEXITCODE"
}

$BuiltInstaller = Join-Path $Repository "dist\installer\VANIOR PRINT Independent Setup v$Version.exe"
if (-not (Test-Path -LiteralPath $BuiltInstaller -PathType Leaf)) {
    throw "Built installer was not found: $BuiltInstaller"
}
if (-not $OutputDirectory) {
    $PersonalBackup = "S:\VANIOR PRINT\3. Бэкап приложения\Установщик"
    $OutputDirectory = if (Test-Path -LiteralPath "S:\VANIOR PRINT") {
        $PersonalBackup
    } else {
        Join-Path ([Environment]::GetFolderPath("UserProfile")) "Downloads"
    }
}
[IO.Directory]::CreateDirectory($OutputDirectory) | Out-Null
$PublishedInstaller = Join-Path $OutputDirectory ([IO.Path]::GetFileName($BuiltInstaller))
Copy-Item -LiteralPath $BuiltInstaller -Destination $PublishedInstaller -Force
$ChecksumSource = "$BuiltInstaller.sha256"
if (Test-Path -LiteralPath $ChecksumSource -PathType Leaf) {
    Copy-Item -LiteralPath $ChecksumSource -Destination "$PublishedInstaller.sha256" -Force
}
$ManifestSource = Join-Path $Repository "dist\installer\VANIOR PRINT v$Version release.json"
if (Test-Path -LiteralPath $ManifestSource -PathType Leaf) {
    Copy-Item -LiteralPath $ManifestSource -Destination $OutputDirectory -Force
}
$Hash = (Get-FileHash -LiteralPath $PublishedInstaller -Algorithm SHA256).Hash
Write-Host ""
Write-Host "VANIOR PRINT installer is ready"
Write-Host "Version: $Version"
Write-Host "File: $PublishedInstaller"
Write-Host "SHA256: $Hash"
