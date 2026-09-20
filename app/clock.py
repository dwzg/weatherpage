"""Time handling — the one place that knows the timestamp convention.

Readings are stored as **naive local-time strings** in ``YYYY-MM-DD HH:MM:SS``
form, exactly as Home Assistant sends them. Nothing is stored in UTC and no
row carries an offset, so every range query is a lexicographic string
comparison. That works only because the format is fixed-width and
zero-padded: keep any new query on this path rather than formatting
timestamps ad hoc.

Known limitation: during the autumn DST fallback the local hour repeats, so
two distinct instants share a timestamp and sort as equal. Fixing that means
migrating the stored format, which is out of scope here.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .config import get_settings

#: Storage format for reading timestamps.
TS_FORMAT = "%Y-%m-%d %H:%M:%S"
DATE_FORMAT = "%Y-%m-%d"

#: Periods the history/stats endpoints accept, and how far back each reaches.
#: ``None`` means "no lower bound" (all of history).
PERIOD_DELTAS: dict[str, timedelta | None] = {
    "3h": timedelta(hours=3),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "today": None,  # handled specially: midnight local
    "all": None,
}

#: Valid values for the ``period`` query parameter, as a regex alternation.
PERIOD_PATTERN = "^(3h|24h|7d|30d|today|all)$"


def now() -> datetime:
    """Current time in the configured timezone (aware)."""
    return datetime.now(tz=get_settings().timezone)


def fmt_ts(dt: datetime) -> str:
    """Format a datetime into the stored timestamp format.

    The tzinfo is dropped rather than converted: callers pass a datetime that
    is already in local time, and the database holds naive local strings.
    """
    return dt.replace(tzinfo=None).strftime(TS_FORMAT)


def fmt_date(dt: datetime) -> str:
    """Format a datetime as a ``YYYY-MM-DD`` day key."""
    return dt.replace(tzinfo=None).strftime(DATE_FORMAT)


def normalise_ts(timestamp: str) -> str:
    """Normalise an incoming timestamp to the stored format.

    Accepts the ISO ``T`` separator and an optional sub-second part or
    timezone suffix, both of which Home Assistant occasionally includes.
    Raises ``ValueError`` if the value is not a timestamp at all.
    """
    parsed = datetime.fromisoformat(timestamp.strip())
    return fmt_ts(parsed)


def start_of_day(dt: datetime) -> datetime:
    """Midnight at the start of ``dt``'s day."""
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def period_cutoff(period: str, reference: datetime | None = None) -> datetime | None:
    """Return the lower bound for ``period``, or ``None`` for an open range.

    ``reference`` defaults to the current local time; tests pass it explicitly.
    """
    ref = reference if reference is not None else now()
    if period == "today":
        return start_of_day(ref)
    delta = PERIOD_DELTAS.get(period)
    return ref - delta if delta is not None else None


def months_ago(count: int, reference: datetime | None = None) -> datetime:
    """Midnight on the first day of the month ``count`` months back.

    Calendar-accurate, unlike multiplying by 30 days.
    """
    ref = reference if reference is not None else now()
    total = ref.year * 12 + (ref.month - 1) - count
    year, month = divmod(total, 12)
    return ref.replace(
        year=year, month=month + 1, day=1, hour=0, minute=0, second=0, microsecond=0
    )
