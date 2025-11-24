from __future__ import annotations

from typing import Optional, Dict


def parse_gps_payload(payload: str) -> Optional[Dict[str, float]]:
    """Parse a GPS payload of the form:
    pos,<lat>,<lon>,<alt>,<age_ms>

    Returns dict with keys lat, lon, alt, age_ms if successful, else None.
    """
    if not isinstance(payload, str):
        return None
    s = payload.strip()
    if not s.lower().startswith("pos,"):
        return None
    parts = s.split(',')
    if len(parts) < 5:
        return None
    try:
        lat = float(parts[1])
        lon = float(parts[2])
        alt = float(parts[3]) if parts[3] != '' else 0.0
        age_ms = int(float(parts[4]))  # tolerate "123.0"
    except (ValueError, TypeError):
        return None

    return {
        "lat": lat,
        "lon": lon,
        "alt": alt,
        "age_ms": age_ms,
    }

