# Gera dist\BIMcloudBackup-Setup.exe com o Inno Setup, a partir de dist\BIMcloudBackup.exe.
# Uso: .\scripts\build_installer.ps1 [-Required]
# Procura o ISCC.exe na variável ISCC, no PATH e nas pastas padrão do Inno Setup 6. Sem ele, só
# avisa e sai sem erro, porque o instalador é opcional no build local; com -Required, falha.

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
    $message = "Inno Setup não encontrado: o instalador não foi gerado (instale o Inno Setup 6 ou rode scripts\install_inno.ps1)."
    if ($Required) { throw $message }
    Write-Warning $message
    exit 0
}

if (-not (Test-Path "dist\BIMcloudBackup.exe")) { throw "Gere antes o dist\BIMcloudBackup.exe" }

# Versão lida do pacote (src\bimcloud_backup\__init__.py), a mesma do executável.
$init = Get-Content "src\bimcloud_backup\__init__.py" -Raw
$match = [regex]::Match($init, '__version__ = "([^"]+)"')
if (-not $match.Success) { throw "Versão não encontrada em src\bimcloud_backup\__init__.py" }
$version = $match.Groups[1].Value
# O Windows só aceita números no campo de versão do arquivo: 0.2.0rc1 -> 0.2.0.
$numeric = [regex]::Match($version, '^\d+(\.\d+){0,3}').Value

& $iscc /Q "/DAppVersion=$version" "/DAppVersionNumeric=$numeric" "installer\BIMcloudBackup.iss"
if ($LASTEXITCODE -ne 0) { throw "Falha ao gerar o instalador" }

Write-Host "Instalador gerado em dist\BIMcloudBackup-Setup.exe (versão $version)"
