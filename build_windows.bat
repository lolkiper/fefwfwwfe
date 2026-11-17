@echo off
cd /d "%~dp0"
py -3 -m pip install -r requirements.txt pyinstaller
py -3 -m PyInstaller --noconfirm --windowed --name DramaStill --collect-all dramastill --add-data "dramastill\resources;dramastill\resources" -m dramastill
echo The executable is in dist\DramaStill\
echo Place ffmpeg.exe and ffprobe.exe next to it or set their paths in the video settings.
