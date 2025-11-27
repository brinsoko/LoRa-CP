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
    "teams_number_header": "Številka",
    "teams_name_header": "Ime ekipe",
    "teams_org_header": "Rod/Org",
    "teams_points_header": "Skupne točke",
    "score_group_header": "Skupina",
    "score_number_header": "Številka",
    "score_team_header": "Ime ekipe",
    "score_org_header": "Rod/Org",
    "score_dead_time_sum_header": "Mrtvi čas (sum)",
    "score_total_header": "Skupaj točke",
    "score_org_section_header": "Organizacija",
    "score_org_teams_header": "Ekipe",
    "score_org_numbers_header": "Številke",
    "score_org_count_header": "Št ekip",
    "score_org_total_header": "Skupaj točke (org)",
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
