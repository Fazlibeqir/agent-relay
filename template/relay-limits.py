"""Read retry times reported by a CLI. Unknown formats retain the configured retry."""
import calendar
import datetime as dt
import re
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def wait_seconds(text, default=1800, now=None):
    now = now or dt.datetime.now().astimezone()
    zone = re.search(r"\(([A-Za-z_]+/[A-Za-z_/+-]+)\)", text)
    if zone:
        try:
            now = now.astimezone(ZoneInfo(zone[1]))
        except ZoneInfoNotFoundError:
            return default
    relative = re.search(r"(?:try again|resets?|retry) in ([\d\w\s,]+)", text, re.I)
    if relative:
        units = re.findall(r"(\d+)\s*(days?|hours?|hrs?|minutes?|mins?|seconds?|secs?)", relative[1], re.I)
        if units:
            seconds = sum(int(n) * (86400 if u.lower().startswith("d") else 3600 if u.lower().startswith("h")
                                   else 60 if u.lower().startswith("m") else 1) for n, u in units)
            return max(120, seconds + 60)
    date = re.search(r"resets?\s+([A-Za-z]{3})\s+(\d{1,2})(?:,\s*(20\d{2}))?,?\s+(\d{1,2})(?::(\d{2}))?\s*([ap])m", text, re.I)
    clock = re.search(r"(?:try again|resets?|retry) at (\d{1,2})(?::(\d{2}))?\s*([ap])m", text, re.I)
    try:
        if date:
            month = list(calendar.month_abbr).index(date[1].title())
            year = int(date[3] or now.year)
            if not 1 <= int(date[4]) <= 12:
                return default
            hour = int(date[4]) % 12 + (12 if date[6].lower() == "p" else 0)
            target = now.replace(year=year, month=month, day=int(date[2]), hour=hour, minute=int(date[5] or 0), second=0, microsecond=0)
            if target <= now and not date[3]:
                target = target.replace(year=year + 1)
        elif clock:
            if not 1 <= int(clock[1]) <= 12:
                return default
            hour = int(clock[1]) % 12 + (12 if clock[3].lower() == "p" else 0)
            target = now.replace(hour=hour, minute=int(clock[2] or 0), second=0, microsecond=0)
            if target <= now:
                target += dt.timedelta(days=1)
        else:
            return default
        return max(120, int(target.timestamp() - now.timestamp()) + 60)
    except (ValueError, OverflowError):
        return default


if __name__ == "__main__":
    print(wait_seconds(sys.stdin.read(), int(sys.argv[1]) if len(sys.argv) > 1 else 1800))
