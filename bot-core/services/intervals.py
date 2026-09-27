"""Parse and format radar check intervals (30m, 2h, 1.5h)."""
import re

_INTERVAL_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours)?$",
    re.IGNORECASE,
)


def parse_check_interval(raw: str) -> int:
    """
    Parse user input into minutes.
    Examples: 30m, 30 m, 2h, 1.5h, 90 (minutes if no unit).
    Plain integer without unit is treated as minutes (e.g. 90 → every 90 minutes).
    """
    s = (raw or "").strip().lower().replace(" ", "")
    if not s:
        raise ValueError("empty interval")

    m = _INTERVAL_RE.match(s)
    if not m:
        raise ValueError(f"invalid interval: {raw}")

    val = float(m.group(1))
    unit = (m.group(2) or "").lower()

    if unit in ("m", "min", "mins", "minute", "minutes"):
        minutes = int(round(val))
    elif unit in ("h", "hr", "hrs", "hour", "hours"):
        minutes = int(round(val * 60))
    else:
        # no unit → minutes (e.g. 90 = every 90 minutes; use 2h for hours)
        minutes = int(round(val))

    return max(1, minutes)


def format_check_interval(minutes: int) -> str:
    minutes = max(1, int(minutes))
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes / 60
    if abs(hours - round(hours)) < 0.05:
        return f"{int(round(hours))}h"
    return f"{hours:.1f}h"
