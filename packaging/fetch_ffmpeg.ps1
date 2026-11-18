param(
    [string]$Dest = "dist\DramaStill"
)

$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $Dest | Out-Null

$zip = Join-Path $env:TEMP "dramastill-ffmpeg-win64.zip"
$url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
Write-Host "Downloading FFmpeg"
Invoke-WebRequest -Uri $url -OutFile $zip

$extract = Join-Path $env:TEMP "dramastill-ffmpeg-win64"
if (Test-Path $extract) {
    Remove-Item -Recurse -Force $extract
}
Expand-Archive -Path $zip -DestinationPath $extract -Force

$ffmpeg = Get-ChildItem -Path $extract -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1
if (-not $ffmpeg) {
    throw "ffmpeg.exe was not found in the archive"
}
Copy-Item $ffmpeg.FullName (Join-Path $Dest "ffmpeg.exe") -Force
Copy-Item (Join-Path $ffmpeg.DirectoryName "ffprobe.exe") (Join-Path $Dest "ffprobe.exe") -Force

$licenseDir = Join-Path $Dest "ffmpeg-licenses"
New-Item -ItemType Directory -Force -Path $licenseDir | Out-Null
Get-ChildItem -Path $extract -Recurse -File | Where-Object {
    $_.Name -match "^(LICENSE|COPYING)"
} | ForEach-Object {
    Copy-Item $_.FullName (Join-Path $licenseDir $_.Name) -Force
}

$readme = Join-Path $PSScriptRoot "README.txt"
if (Test-Path $readme) {
    Copy-Item $readme (Join-Path $Dest "README.txt") -Force
}
Write-Host "FFmpeg copied to $Dest"
