from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DISPLAY_TIME_MODE = "gmt"
DISPLAY_TIME_LABEL = "GMT"
DEFAULT_TZ_NAME = "UTC"
DEFAULT_TZ = ZoneInfo(DEFAULT_TZ_NAME)


def get_timezone(tz_name: str | None = None) -> ZoneInfo:
    name = (tz_name or DEFAULT_TZ_NAME).strip()
    if name.upper() in {"GMT", "UTC", "ETC/GMT"}:
        return DEFAULT_TZ
    return ZoneInfo(name)


def _as_aware_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_datetime_gmt(dt: datetime | None) -> str:
    if not dt:
        return ""
    return _as_aware_utc(dt).strftime("%Y-%m-%d %H:%M:%S") + f" {DISPLAY_TIME_LABEL}"


def format_datetime_input_gmt(dt: datetime | None) -> str:
    if not dt:
        return ""
    return _as_aware_utc(dt).strftime("%Y-%m-%dT%H:%M:%S")


def _parse_datetime_value(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    for fmt in ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None

def to_datetime_local(dt: datetime, tz: ZoneInfo | None = None) -> str:
    if not dt:
        return ""
    # Legacy template filter. For now the application displays database
    # timestamps in GMT; keep the function name so local-time support can
    # later branch here without touching every template.
    return _as_aware_utc(dt).strftime("%d-%m-%Y %H:%M:%S") + f" {DISPLAY_TIME_LABEL}"

def from_datetime_local(s: str | None, tz_name: str | None = None) -> datetime | None:
    if not s:
        return None
    tz = get_timezone(tz_name)
    local_dt = _parse_datetime_value(s)
    if local_dt is None:
        return None
    if local_dt.tzinfo is not None:
        return local_dt.astimezone(timezone.utc).replace(tzinfo=None)
    aware_local = local_dt.replace(tzinfo=tz)
    utc_dt = aware_local.astimezone(timezone.utc)
    return utc_dt.replace(tzinfo=None)
