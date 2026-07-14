from datetime import UTC, datetime
from zoneinfo import ZoneInfo

# Storage stays UTC everywhere in the database.
# Display happens in this single timezone (server-side conversion).
STORAGE_TZ = UTC
DISPLAY_TZ_NAME = "Europe/Ljubljana"
DISPLAY_TZ = ZoneInfo(DISPLAY_TZ_NAME)

# Default timezone used when interpreting form-submitted local times.
DEFAULT_TZ_NAME = DISPLAY_TZ_NAME
DEFAULT_TZ = DISPLAY_TZ


def utcnow_naive() -> datetime:
    """Naive UTC `datetime.now()`.

    Replaces `datetime.utcnow()`, which is deprecated in Python 3.12+. The
    return value matches the existing convention in this codebase: naive
    datetimes interpreted as UTC for storage in DateTime columns.
    """
    return datetime.now(UTC).replace(tzinfo=None)


def utc_from_timestamp_naive(ts: float) -> datetime:
    """Naive UTC datetime from a unix timestamp.

    Replaces `datetime.utcfromtimestamp(ts)`, deprecated in Python 3.12+.
    """
    return datetime.fromtimestamp(ts, tz=UTC).replace(tzinfo=None)


def get_timezone(tz_name: str | None = None) -> ZoneInfo:
    name = (tz_name or DEFAULT_TZ_NAME).strip()
    if name.upper() in {"GMT", "UTC", "ETC/GMT"}:
        return ZoneInfo("UTC")
    return ZoneInfo(name)


def _as_aware_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _as_display(dt: datetime) -> datetime:
    return _as_aware_utc(dt).astimezone(DISPLAY_TZ)


def format_datetime_display(dt: datetime | None) -> str:
    if not dt:
        return ""
    return _as_display(dt).strftime("%Y-%m-%d %H:%M:%S")


def format_time_display(dt: datetime | None) -> str:
    """Clock time only (HH:MM:SS) in the display timezone.

    For compact cells (e.g. time-trial arrival times) where the date is
    implied by the competition day.
    """
    if not dt:
        return ""
    return _as_display(dt).strftime("%H:%M:%S")


def format_datetime_input_local(dt: datetime | None) -> str:
    if not dt:
        return ""
    return _as_display(dt).strftime("%Y-%m-%dT%H:%M:%S")


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
    target = tz or DISPLAY_TZ
    return _as_aware_utc(dt).astimezone(target).strftime("%d-%m-%Y %H:%M:%S")


def from_datetime_local(s: str | None, tz_name: str | None = None) -> datetime | None:
    if not s:
        return None
    tz = get_timezone(tz_name)
    local_dt = _parse_datetime_value(s)
    if local_dt is None:
        return None
    if local_dt.tzinfo is not None:
        return local_dt.astimezone(UTC).replace(tzinfo=None)
    aware_local = local_dt.replace(tzinfo=tz)
    utc_dt = aware_local.astimezone(UTC)
    return utc_dt.replace(tzinfo=None)
