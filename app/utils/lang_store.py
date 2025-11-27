import json
from pathlib import Path
from typing import Dict

from flask import current_app

DEFAULT_LANG = {
    "arrived_header": "prihod na KT",
    "points_header": "Točke",
    "dead_time_header": "Mrtvi čas [min]",
    "time_header": "Čas",
    "teams_tab": "Ekipe",
    "arrivals_tab": "Prihodi",
    "score_tab": "Skupni seštevek",
}


def _lang_path() -> Path:
    inst = current_app.instance_path
    return Path(inst) / "sheets_lang.json"


def load_lang() -> Dict[str, str]:
    path = _lang_path()
    if not path.exists():
        return DEFAULT_LANG.copy()
    try:
        data = json.loads(path.read_text())
        return {**DEFAULT_LANG, **(data or {})}
    except Exception:
        return DEFAULT_LANG.copy()


def save_lang(payload: Dict[str, str]) -> None:
    path = _lang_path()
    merged = {**DEFAULT_LANG, **(payload or {})}
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2))
