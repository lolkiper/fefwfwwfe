# Сборка portable + установщик Inno Setup (Windows)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

python -m pip install -r requirements-build.txt
python -m PyInstaller --noconfirm twitch-drops-unlink.spec

$inno = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (Test-Path $inno) {
    & $inno "installer\TwitchDropsUnlink.iss"
    Write-Host "Installer: release\TwitchDropsUnlink-Setup-1.2.0.exe"
} else {
    Write-Host "Inno Setup не найден. Portable: dist\TwitchDropsUnlink\"
}