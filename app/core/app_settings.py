"""Small app-wide preferences (not profiles), stored as JSON. Never holds secrets."""

from __future__ import annotations

import json
from pathlib import Path

from .paths import app_data_dir


def settings_path() -> Path:
    return app_data_dir() / "settings.json"


def load_settings() -> dict:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def get_setting(key: str, default: str = "") -> str:
    value = load_settings().get(key, default)
    return value if isinstance(value, str) else default


def set_setting(key: str, value: str) -> None:
    data = load_settings()
    data[key] = value
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary.replace(path)
