# Builds dist\BIMcloudBackup.exe (a single file, with no console window) and, when Inno Setup is
# installed, also dist\BIMcloudBackup-Setup.exe (scripts\build_installer.ps1).
# Usage: .\scripts\build_exe.ps1   (with the virtual environment active)

Set-Location (Join-Path $PSScriptRoot "..")

python -m pip install --quiet --upgrade --require-hashes -r requirements-pip.txt
if ($LASTEXITCODE -ne 0) { throw "Could not upgrade pip" }

python -m pip install --quiet --require-hashes -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw "Could not install the checked dependencies" }

python -m pip install --quiet --no-deps --no-build-isolation -e .
if ($LASTEXITCODE -ne 0) { throw "Could not install the program" }

python -m PyInstaller `
    --noconfirm `
    --clean `
    --log-level WARN `
    --noupx `
    --onefile `
    --windowed `
    --name BIMcloudBackup `
    --icon src\bimcloud_backup\assets\icon.ico `
    --paths src `
    --collect-data sv_ttk `
    --collect-data bimcloud_backup `
    src\bimcloud_backup\__main__.py
if ($LASTEXITCODE -ne 0) { throw "Could not build the executable" }

Write-Host "Executable built in dist\BIMcloudBackup.exe"

& (Join-Path $PSScriptRoot "build_installer.ps1")
