#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
    echo "Creating virtual environment..."
    if command -v python3 >/dev/null 2>&1; then
        python3 -m venv .venv
    else
        python -m venv .venv
    fi
fi

. ".venv/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install --upgrade yt-dlp
python app.py
