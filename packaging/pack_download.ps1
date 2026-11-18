param(
    [string]$Source = "dist\DramaStill",
    [string]$OutZip = "DramaStill-windows.zip",
    [string]$Password = "dramastill"
)

$ErrorActionPreference = "Stop"
$sevenZip = @(
    "C:\Program Files\7-Zip\7z.exe",
    "C:\Program Files (x86)\7-Zip\7z.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $sevenZip) {
    throw "7-Zip not found"
}
if (-not (Test-Path (Join-Path $Source "DramaStill.exe"))) {
    throw "DramaStill.exe is missing in $Source"
}

$stage = Join-Path $env:TEMP "dramastill-download-pack"
if (Test-Path $stage) {
    Remove-Item -Recurse -Force $stage
}
New-Item -ItemType Directory -Force -Path $stage | Out-Null
Copy-Item (Join-Path $PSScriptRoot "OPEN-ME.txt") (Join-Path $stage "README.txt") -Force

$archive = Join-Path $stage "DramaStill.7z"
$sourceParent = Split-Path -Parent (Resolve-Path $Source)
$sourceName = Split-Path -Leaf (Resolve-Path $Source)
Push-Location $sourceParent
try {
    & $sevenZip a -t7z -mhe=on "-p$Password" $archive $sourceName
}
finally {
    Pop-Location
}
if ($LASTEXITCODE -ne 0) {
    throw "7-Zip failed to create DramaStill.7z"
}

if (Test-Path $OutZip) {
    Remove-Item -Force $OutZip
}
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $OutZip -Force

Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::OpenRead((Resolve-Path $OutZip))
try {
    $names = @($zip.Entries | ForEach-Object { $_.FullName })
    $dangerous = @($names | Where-Object { $_ -match '\.(exe|dll|bat|cmd|scr|msi|ps1)$' })
    if ($dangerous.Count -gt 0) {
        throw "Outer archive still contains executable files: $($dangerous -join ', ')"
    }
    if (-not ($names | Where-Object { $_ -eq "DramaStill.7z" })) {
        throw "DramaStill.7z is missing from the outer archive"
    }
    if (-not ($names | Where-Object { $_ -eq "README.txt" })) {
        throw "README.txt is missing from the outer archive"
    }
}
finally {
    $zip.Dispose()
}

Write-Host "Created $OutZip. Password for DramaStill.7z: $Password"
