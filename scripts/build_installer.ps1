# Builds dist\BIMcloudBackup-Setup.exe with Inno Setup, from dist\BIMcloudBackup.exe.
# Usage: .\scripts\build_installer.ps1 [-Required]
# Looks for ISCC.exe in the ISCC variable, in the PATH and in the default Inno Setup 6 folders.
# Without it, only warns and exits with no error, since the installer is optional in a local
# build; with -Required, fails.

param([switch]$Required)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$candidates = @(
    $env:ISCC,
    (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source,
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
)
$iscc = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $iscc) {
    $message = "Inno Setup not found: the installer was not built (install Inno Setup 6 or run scripts\install_inno.ps1)."
    if ($Required) { throw $message }
    Write-Warning $message
    exit 0
}

if (-not (Test-Path "dist\BIMcloudBackup.exe")) { throw "Build dist\BIMcloudBackup.exe first" }

# Version read from the package (src\bimcloud_backup\__init__.py), the same as the executable's.
$init = Get-Content "src\bimcloud_backup\__init__.py" -Raw
$match = [regex]::Match($init, '__version__ = "([^"]+)"')
if (-not $match.Success) { throw "Version not found in src\bimcloud_backup\__init__.py" }
$version = $match.Groups[1].Value
# Windows only takes numbers in the file version field: 0.2.0rc1 -> 0.2.0.
$numeric = [regex]::Match($version, '^\d+(\.\d+){0,3}').Value

& $iscc /Q "/DAppVersion=$version" "/DAppVersionNumeric=$numeric" "installer\BIMcloudBackup.iss"
if ($LASTEXITCODE -ne 0) { throw "Could not build the installer" }

Write-Host "Installer built in dist\BIMcloudBackup-Setup.exe (version $version)"
