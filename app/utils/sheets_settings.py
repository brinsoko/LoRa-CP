import json
from pathlib import Path

from flask import current_app

DEFAULT_SETTINGS = {
    "sync_enabled": True,
}


def _settings_path() -> Path:
    inst = current_app.instance_path
    return Path(inst) / "sheets_settings.json"


def load_settings() -> dict:
    path = _settings_path()
    if not path.exists():
        settings = DEFAULT_SETTINGS.copy()
        cfg_default = current_app.config.get("SHEETS_SYNC_ENABLED")
        if cfg_default is not None:
            settings["sync_enabled"] = bool(cfg_default)
        return settings
    try:
        data = json.loads(path.read_text())
    except Exception:
        return DEFAULT_SETTINGS.copy()
    merged = {**DEFAULT_SETTINGS, **(data or {})}
    merged["sync_enabled"] = bool(merged.get("sync_enabled", True))
    return merged


def save_settings(payload: dict) -> None:
    path = _settings_path()
    merged = {**DEFAULT_SETTINGS, **(payload or {})}
    merged["sync_enabled"] = bool(merged.get("sync_enabled", True))
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2))


def sheets_sync_enabled() -> bool:
    settings = load_settings()
    return bool(settings.get("sync_enabled", True))
