"""Parsing and formatting the times people give for entries."""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def parse_time(value: str | None, tz: ZoneInfo) -> datetime:
    """Parse 'now', '15m'/'2h' (ago), 'HH:MM' (most recent such time), or ISO 8601.

    Raises ValueError with a message suitable for the user.
    """
    now = datetime.now(tz)
    if not value or value.strip().lower() == "now":
        return now
    text = value.strip().lower()

    relative = re.fullmatch(r"(\d+)\s*(m|min|mins|minutes?|h|hr|hrs|hours?)(?:\s*ago)?", text)
    if relative:
        amount, unit = int(relative.group(1)), relative.group(2)
        return now - (timedelta(hours=amount) if unit.startswith("h") else timedelta(minutes=amount))

    clock = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if clock:
        hour, minute = int(clock.group(1)), int(clock.group(2))
        if hour > 23 or minute > 59:
            raise ValueError(f"'{value}' is not a valid time of day")
        at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        # "02:30" said at 08:00 means this morning; said at 01:00 it means last night
        return at - timedelta(days=1) if at > now else at

    try:
        at = datetime.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(f"Can't read time '{value}'; use now, 15m, 2h, HH:MM, or ISO 8601") from None
    return at if at.tzinfo else at.replace(tzinfo=tz)


def show_time(at: datetime | float, tz: ZoneInfo) -> str:
    """A readable local time, e.g. 'Mon Oct 19 3:10 AM EDT'. Numbers are epoch seconds."""
    if not isinstance(at, datetime):
        at = datetime.fromtimestamp(at, tz)
    return at.astimezone(tz).strftime("%a %b %-d %-I:%M %p %Z")
