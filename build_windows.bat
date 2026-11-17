@echo off
cd /d "%~dp0"
py -3 -m pip install -r requirements.txt pyinstaller
if errorlevel 1 exit /b 1
py -3 -m PyInstaller --noconfirm --clean DramaStill.spec
if errorlevel 1 exit /b 1
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\fetch_ffmpeg.ps1 -Dest dist\DramaStill
if errorlevel 1 exit /b 1
powershell -NoProfile -Command "Compress-Archive -Path dist\DramaStill -DestinationPath DramaStill-windows.zip -Force"
echo.
echo DramaStill.exe is in dist\DramaStill\
echo Archive: DramaStill-windows.zip
