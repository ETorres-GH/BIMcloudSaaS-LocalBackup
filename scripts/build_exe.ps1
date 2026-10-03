# Gera dist\BIMcloudBackup.exe (um único arquivo, sem janela de console) e, se o Inno Setup
# estiver instalado, também dist\BIMcloudBackup-Setup.exe (scripts\build_installer.ps1).
# Uso: .\scripts\build_exe.ps1   (com o ambiente virtual ativado)

Set-Location (Join-Path $PSScriptRoot "..")

python -m pip install --quiet --upgrade --require-hashes -r requirements-pip.txt
if ($LASTEXITCODE -ne 0) { throw "Falha ao atualizar o pip" }

python -m pip install --quiet --require-hashes -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw "Falha ao instalar as dependências conferidas" }

python -m pip install --quiet --no-deps --no-build-isolation -e .
if ($LASTEXITCODE -ne 0) { throw "Falha ao instalar o programa" }

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
if ($LASTEXITCODE -ne 0) { throw "Falha ao gerar o executável" }

Write-Host "Executável gerado em dist\BIMcloudBackup.exe"

& (Join-Path $PSScriptRoot "build_installer.ps1")
