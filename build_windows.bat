@echo off
cd /d "%~dp0"
py -3 -m pip install -r requirements.txt pyinstaller
if errorlevel 1 exit /b 1
py -3 -m PyInstaller --noconfirm --clean DramaStill.spec
if errorlevel 1 exit /b 1
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\fetch_ffmpeg.ps1 -Dest dist\DramaStill
if errorlevel 1 exit /b 1
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\pack_download.ps1 -Source dist\DramaStill -OutZip DramaStill-windows.zip
if errorlevel 1 exit /b 1
echo.
echo DramaStill.exe is in dist\DramaStill\
echo Download archive: DramaStill-windows.zip
echo Inner archive password: dramastill
