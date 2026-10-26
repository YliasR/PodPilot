# Builds dist\PodPilot\PodPilot.exe and dist\PodPilot-Setup-<version>.exe (+ SHA256SUMS.txt).
# Needs: pip install -r requirements.txt pyinstaller, and Inno Setup 6.
param([switch]$SkipInstaller)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$version = (Select-String -Path podpilot\__init__.py -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
Write-Host "Building PodPilot $version"

python -m PyInstaller --noconfirm --clean packaging\PodPilot.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
if ($SkipInstaller) { return }

$iscc = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 not found (winget install JRSoftware.InnoSetup)" }

& $iscc /Q "/DAppVersion=$version" packaging\PodPilot.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }

$setup = "dist\PodPilot-Setup-$version.exe"
$hash = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
"$hash  PodPilot-Setup-$version.exe" | Set-Content -Encoding ascii dist\SHA256SUMS.txt
Write-Host "Built $setup"
Write-Host "SHA256 $hash"
