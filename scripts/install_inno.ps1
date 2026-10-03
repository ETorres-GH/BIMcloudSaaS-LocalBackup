# Instala o Inno Setup numa pasta própria, com versão fixa e o arquivo conferido antes de rodar.
# Uso (GitHub Actions ou local):  .\scripts\install_inno.ps1 [-Destination <pasta>]
# Imprime o caminho do ISCC.exe e, no GitHub Actions, grava ISCC=<caminho> em GITHUB_ENV.
# O Inno Setup é gratuito, inclusive para uso comercial (https://jrsoftware.org/files/is/license.txt).

param([string]$Destination)

$ErrorActionPreference = "Stop"
# Compatível também com o Windows PowerShell 5.1.
$temp = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }
if (-not $Destination) { $Destination = Join-Path $temp "InnoSetup" }

$Version = "6.7.3"
# SHA-256 publicado pelo GitHub para o anexo da release oficial (jrsoftware/issrc, is-6_7_3).
$Sha256 = "9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732"
$Url = "https://github.com/jrsoftware/issrc/releases/download/is-$($Version.Replace('.', '_'))/innosetup-$Version.exe"
$Publisher = "Pyrsys B.V."

$setup = Join-Path $temp "innosetup-$Version.exe"
Invoke-WebRequest -Uri $Url -OutFile $setup -UseBasicParsing

$actual = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
if ($actual -ne $Sha256) { throw "SHA-256 do Inno Setup não confere: $actual" }

$signature = Get-AuthenticodeSignature $setup
if ($signature.Status -ne "Valid" -or $signature.SignerCertificate.Subject -notmatch [regex]::Escape($Publisher)) {
    throw "Assinatura do Inno Setup inválida: $($signature.Status) $($signature.SignerCertificate.Subject)"
}

$process = Start-Process -FilePath $setup -Wait -PassThru -ArgumentList @(
    "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/CURRENTUSER", "/NOICONS",
    "/DIR=`"$Destination`""
)
if ($process.ExitCode -ne 0) { throw "Falha ao instalar o Inno Setup (código $($process.ExitCode))" }

$iscc = Join-Path $Destination "ISCC.exe"
if (-not (Test-Path $iscc)) { throw "ISCC.exe não encontrado em $Destination" }
if ($env:GITHUB_ENV) { "ISCC=$iscc" | Out-File -FilePath $env:GITHUB_ENV -Append -Encoding utf8 }
Write-Output $iscc
