@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller

".venv\Scripts\python.exe" -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --windowed ^
  --name "YouTube Downloader GUI" ^
  --collect-all yt_dlp ^
  app.py

echo.
echo Build complete: dist\YouTube Downloader GUI\YouTube Downloader GUI.exe
pause
