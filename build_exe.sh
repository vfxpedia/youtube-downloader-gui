#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  python3 -m venv .venv
fi

".venv/bin/python" -m pip install --upgrade pip
".venv/bin/python" -m pip install -r requirements.txt pyinstaller

".venv/bin/python" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name "YouTube Downloader GUI" \
  --collect-all yt_dlp \
  app.py

echo "Build complete: dist/YouTube Downloader GUI"
