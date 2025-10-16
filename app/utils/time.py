from datetime import datetime
from zoneinfo import ZoneInfo

DEFAULT_TZ = ZoneInfo("Europe/Ljubljana")

def to_datetime_local(dt: datetime, tz: ZoneInfo = DEFAULT_TZ) -> str:
    if not dt:
        return ""
    local_dt = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    return local_dt.strftime("%Y-%m-%dT%H:%M:%S")

def from_datetime_local(s: str | None, tz_name: str | None = None) -> datetime | None:
    if not s:
        return None
    tz = ZoneInfo(tz_name) if tz_name else DEFAULT_TZ
    try:
        local_dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        try:
            local_dt = datetime.strptime(s, "%Y-%m-%dT%H:%M")
        except ValueError:
            return None
    aware_local = local_dt.replace(tzinfo=tz)
    utc_dt = aware_local.astimezone(ZoneInfo("UTC"))
    return utc_dt.replace(tzinfo=None)