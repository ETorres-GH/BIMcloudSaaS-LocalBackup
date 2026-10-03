# Installs Inno Setup in a folder of its own, with a pinned version and the file checked first.
# Usage (GitHub Actions or local):  .\scripts\install_inno.ps1 [-Destination <folder>]
# Prints the path of ISCC.exe and, on GitHub Actions, writes ISCC=<path> to GITHUB_ENV.
# Inno Setup is free, commercial use included (https://jrsoftware.org/files/is/license.txt).

param([string]$Destination)

$ErrorActionPreference = "Stop"
# Works with Windows PowerShell 5.1 too.
$temp = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }
if (-not $Destination) { $Destination = Join-Path $temp "InnoSetup" }

$Version = "6.7.3"
# SHA-256 published by GitHub for the asset of the official release (jrsoftware/issrc, is-6_7_3).
$Sha256 = "9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732"
$Url = "https://github.com/jrsoftware/issrc/releases/download/is-$($Version.Replace('.', '_'))/innosetup-$Version.exe"
$Publisher = "Pyrsys B.V."

$setup = Join-Path $temp "innosetup-$Version.exe"
Invoke-WebRequest -Uri $Url -OutFile $setup -UseBasicParsing

$actual = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
if ($actual -ne $Sha256) { throw "Inno Setup SHA-256 does not match: $actual" }

$signature = Get-AuthenticodeSignature $setup
if ($signature.Status -ne "Valid" -or $signature.SignerCertificate.Subject -notmatch [regex]::Escape($Publisher)) {
    throw "Invalid Inno Setup signature: $($signature.Status) $($signature.SignerCertificate.Subject)"
}

$process = Start-Process -FilePath $setup -Wait -PassThru -ArgumentList @(
    "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/CURRENTUSER", "/NOICONS",
    "/DIR=`"$Destination`""
)
if ($process.ExitCode -ne 0) { throw "Could not install Inno Setup (code $($process.ExitCode))" }

$iscc = Join-Path $Destination "ISCC.exe"
if (-not (Test-Path $iscc)) { throw "ISCC.exe not found in $Destination" }
if ($env:GITHUB_ENV) { "ISCC=$iscc" | Out-File -FilePath $env:GITHUB_ENV -Append -Encoding utf8 }
Write-Output $iscc
