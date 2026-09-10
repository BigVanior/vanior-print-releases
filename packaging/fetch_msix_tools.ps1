param(
    [string]$Version = "10.0.28000.2705"
)

$ErrorActionPreference = "Stop"
$Repository = Split-Path -Parent $PSScriptRoot
$ToolsRoot = Join-Path $Repository ".build-tools\msix\$Version"
$ExpectedPackageHash = "8BFDFB6CA2633F531CF80B5FA22512BA61A394D7988F0970DB83BAADC67929ED"
$PackageName = "microsoft.windows.sdk.buildtools.$Version.nupkg"
$PackagePath = Join-Path $ToolsRoot $PackageName
$MakeAppx = Get-ChildItem -LiteralPath $ToolsRoot -Filter "makeappx.exe" -File -Recurse -ErrorAction SilentlyContinue |
    Where-Object DirectoryName -Match '\\x64$' |
    Select-Object -First 1
if ($MakeAppx) {
    Write-Output $MakeAppx.FullName
    exit 0
}

[IO.Directory]::CreateDirectory($ToolsRoot) | Out-Null
$Uri = "https://api.nuget.org/v3-flatcontainer/microsoft.windows.sdk.buildtools/$Version/$PackageName"
if (-not (Test-Path -LiteralPath $PackagePath -PathType Leaf)) {
    Invoke-WebRequest -Uri $Uri -OutFile $PackagePath
}
$ActualHash = (Get-FileHash -LiteralPath $PackagePath -Algorithm SHA256).Hash
if ($ActualHash -ne $ExpectedPackageHash) {
    Remove-Item -LiteralPath $PackagePath -Force
    throw "Downloaded Windows SDK Build Tools package failed SHA-256 verification."
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::ExtractToDirectory($PackagePath, $ToolsRoot, $true)
$MakeAppx = Get-ChildItem -LiteralPath $ToolsRoot -Filter "makeappx.exe" -File -Recurse |
    Where-Object DirectoryName -Match '\\x64$' |
    Select-Object -First 1
if (-not $MakeAppx) {
    throw "The verified Windows SDK Build Tools package does not contain x64 makeappx.exe."
}
Write-Output $MakeAppx.FullName
