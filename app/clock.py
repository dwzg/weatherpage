"""Time handling — the one place that knows the timestamp convention.

Readings are stored as **naive local-time strings** in ``YYYY-MM-DD HH:MM:SS``
form, exactly as Home Assistant sends them. Nothing is stored in UTC, so
every range query is a lexicographic string comparison. That works only because the format is fixed-width and
zero-padded: keep any new query on this path rather than formatting
timestamps ad hoc.

During the autumn DST fallback the local hour repeats, so two distinct
instants share a timestamp. The stored string cannot tell them apart, so each
reading also carries the UTC offset its instant was taken at
(:func:`resolve_offset`). That keeps the string fixed-width and
lexicographically comparable — the whole reason for the format — while still
distinguishing the two passes through the repeated hour and ordering them.
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


def offset_minutes(dt: datetime) -> int:
    """An aware datetime's offset from UTC, in minutes east."""
    utcoffset = dt.utcoffset()
    return 0 if utcoffset is None else int(utcoffset.total_seconds() // 60)


def resolve_offset(timestamp: str, arrival: datetime | None = None) -> int:
    """Minutes east of UTC for the instant ``timestamp`` refers to.

    The stored string is a local wall clock, which is ambiguous for one hour
    each autumn: 02:30 happens twice, once at +02:00 and again at +01:00.
    Everywhere else the two folds agree and there is nothing to decide.

    Three things are consulted, in order:

    1. An explicit offset on the incoming value, when it is one the zone
       actually uses at that wall time. This is the only real information
       there is, so it wins — and it is the way a backfill can aim at a
       particular pass through the repeated hour.
    2. ``arrival``, the instant the reading reached us, which live ingestion
       passes and nothing else can know. A reading arrives minutes after it
       was taken, so once the second pass has begun a bare 02:30 means the
       second 02:30 — the first one is already stored.
    3. Otherwise the first pass, matching Python's own ``fold=0`` default.

    Leaving ``arrival`` out therefore makes this a pure function of the
    timestamp, which is what the migration reading old rows needs. It is also
    why :mod:`backfill` cannot restore the first pass of a repeated hour after
    the fact: walking wall-clock time only ever visits 02:30 once, and the
    replay has no arrival time to go on. Putting the offset in the timestamp
    is the way around that.

    Raises ``ValueError`` if the value is not a timestamp at all.
    """
    parsed = datetime.fromisoformat(timestamp.strip())
    tz = get_settings().timezone
    naive = parsed.replace(tzinfo=None)

    # PEP 495: for a repeated hour fold=0 is the earlier instant and so the
    # larger offset; for the spring gap the order is the other way round and
    # the local time never happened, in which case fold=0 is as good an answer
    # as any.
    first = naive.replace(tzinfo=tz, fold=0)
    second = naive.replace(tzinfo=tz, fold=1)
    first_offset, second_offset = offset_minutes(first), offset_minutes(second)

    if parsed.tzinfo is not None:
        given = offset_minutes(parsed)
        if given in (first_offset, second_offset):
            return given

    if first_offset <= second_offset:
        return first_offset

    # Compared as instants: two aware datetimes in the same zone compare
    # equal when only their fold differs (PEP 495), which is exactly the pair
    # being told apart here.
    if arrival is not None and arrival.timestamp() >= second.timestamp():
        return second_offset
    return first_offset


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
