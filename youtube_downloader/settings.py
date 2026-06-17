from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


APP_CONFIG_DIR = Path(os.getenv("APPDATA", Path.home())) / "YoutubeDownloaderGui"
SETTINGS_PATH = APP_CONFIG_DIR / "settings.json"


DEFAULT_SETTINGS: dict[str, Any] = {
    "last_output_dir": str(Path.home() / "Downloads"),
    "last_mode": "video",
}


def load_settings() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return DEFAULT_SETTINGS.copy()

    try:
        with SETTINGS_PATH.open("r", encoding="utf-8") as file:
            loaded = json.load(file)
    except (OSError, json.JSONDecodeError):
        return DEFAULT_SETTINGS.copy()

    settings = DEFAULT_SETTINGS.copy()
    if isinstance(loaded, dict):
        settings.update({key: value for key, value in loaded.items() if key in settings})
    return settings


def save_settings(settings: dict[str, Any]) -> None:
    APP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = DEFAULT_SETTINGS.copy()
    data.update({key: value for key, value in settings.items() if key in data})

    with SETTINGS_PATH.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
