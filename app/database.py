"""All SQLite access.

Schema is a single ``weather_readings`` table; everything else is an
aggregate query over it. Connections come from a small pool opened at
startup (see :func:`connect` / :func:`disconnect`) rather than being created
per call, so a page render does not pay a dozen connection setups.

Timestamps follow the convention documented in :mod:`app.clock`: naive
local-time strings compared lexicographically. Build every bound with
``clock.fmt_ts`` so comparisons stay on that path.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import aiosqlite

from . import clock
from .cache import cached
from .cache import invalidate as invalidate_cache
from .config import READING_INTERVAL_MINUTES, get_settings

log = logging.getLogger(__name__)

#: Metrics stored per reading. Used to build aggregate SQL, so the names must
#: stay in sync with the column names — never interpolate anything else.
METRICS: tuple[str, ...] = ("temperature", "humidity", "pressure")

#: Connections kept open. SQLite serialises writes anyway; a handful of
#: readers is plenty for a single-dashboard app and avoids reconnect cost.
POOL_SIZE = 4

#: Roughly how many points a chart should receive. Longer periods are
#: bucketed to stay near this, instead of shipping every 5-minute row.
TARGET_CHART_POINTS = 1500

#: The window the pressure trend is measured over, in hours.
TREND_WINDOW_HOURS = 6

#: Width of the median window at each end of a trend comparison, in minutes,
#: and the number of readings that spans.
SMOOTHING_WINDOW_MINUTES = 30
SMOOTHING_READINGS = max(int(SMOOTHING_WINDOW_MINUTES / READING_INTERVAL_MINUTES), 1)

#: How far back the daily pressure cycle is learned from.
CYCLE_LEARN_DAYS = 90

#: Readings a day needs before it counts toward the learned cycle — a day
#: with an outage in it would bias the slots that are missing.
CYCLE_MIN_READINGS_PER_DAY = 200

#: Complete days needed before the learned cycle is applied at all.
CYCLE_MIN_DAYS = 14

#: Bucket widths (minutes) tried in order when downsampling.
_BUCKET_LADDER: tuple[int, ...] = (
    READING_INTERVAL_MINUTES, 10, 15, 30, 60, 180, 360, 720, 1440,
)

_pool: asyncio.Queue[aiosqlite.Connection] | None = None
_all_connections: list[aiosqlite.Connection] = []
_write_lock = asyncio.Lock()


# ── Connection handling ────────────────────────────────────────────────────


async def _open_connection() -> aiosqlite.Connection:
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(settings.db_path))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute("PRAGMA busy_timeout=5000")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def connect(pool_size: int = POOL_SIZE) -> None:
    """Migrate the database, then open the pool. Call once at startup.

    The migration runs on its own connection and finishes *before* the pooled
    ones are opened. A connection caches the schema it saw at open time, and
    ``ON CONFLICT(timestamp)`` is resolved when a statement is prepared — so a
    connection opened before the unique index existed would reject every
    upsert for the life of the process.
    """
    global _pool
    if _pool is not None:
        return

    migrator = await _open_connection()
    try:
        await _migrate(migrator)
    finally:
        await migrator.close()

    pool: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue()
    for _ in range(pool_size):
        db = await _open_connection()
        _all_connections.append(db)
        pool.put_nowait(db)
    _pool = pool


async def disconnect() -> None:
    """Close every pooled connection. Call once at shutdown."""
    global _pool
    _pool = None
    for db in _all_connections:
        await db.close()
    _all_connections.clear()


@asynccontextmanager
async def acquire() -> AsyncIterator[aiosqlite.Connection]:
    """Borrow a connection from the pool for the duration of the block."""
    if _pool is None:
        raise RuntimeError("database pool is not open — call connect() first")
    db = await _pool.get()
    try:
        yield db
    finally:
        _pool.put_nowait(db)


async def _fetch_all(sql: str, params: Sequence[Any] = ()) -> list[dict]:
    async with acquire() as db:
        cursor = await db.execute(sql, params)
        return [dict(row) for row in await cursor.fetchall()]


async def _fetch_one(sql: str, params: Sequence[Any] = ()) -> dict | None:
    async with acquire() as db:
        cursor = await db.execute(sql, params)
        row = await cursor.fetchone()
        return dict(row) if row else None


# ── Schema ─────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS weather_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    temperature REAL NOT NULL,
    humidity REAL NOT NULL,
    pressure REAL NOT NULL,
    timestamp TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_timestamp ON weather_readings(timestamp);
"""

_DEDUPE = """
DELETE FROM weather_readings WHERE id NOT IN (
    SELECT MIN(id) FROM weather_readings GROUP BY timestamp
)
"""


async def _migrate(db: aiosqlite.Connection) -> None:
    """Create the schema and make ``timestamp`` unique.

    A unique timestamp lets ingestion upsert instead of appending duplicates,
    which is what the cleanup endpoint used to have to undo by hand. An
    existing database may already contain duplicates, so the index creation
    is attempted first and only falls back to de-duplicating (keeping the
    earliest row per timestamp, exactly as cleanup always has) if it fails.
    """
    await db.executescript(_SCHEMA)
    try:
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_timestamp_unique "
            "ON weather_readings(timestamp)"
        )
    except aiosqlite.IntegrityError:
        cursor = await db.execute(_DEDUPE)
        log.warning(
            "migration: removed %d duplicate reading(s) to make timestamps unique",
            cursor.rowcount,
        )
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_timestamp_unique "
            "ON weather_readings(timestamp)"
        )
    await db.commit()


async def init_db() -> None:
    """Backwards-compatible alias for :func:`connect`."""
    await connect()


# ── Writes ─────────────────────────────────────────────────────────────────


async def insert_reading(
    temperature: float, humidity: float, pressure: float, timestamp: str
) -> int:
    """Store a reading, replacing any existing one with the same timestamp.

    Returns the row id. ``timestamp`` may use the ISO ``T`` separator; it is
    normalised to the stored format.
    """
    ts = clock.normalise_ts(timestamp)
    async with _write_lock, acquire() as db:
        cursor = await db.execute(
            """
            INSERT INTO weather_readings (temperature, humidity, pressure, timestamp)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(timestamp) DO UPDATE SET
                temperature = excluded.temperature,
                humidity    = excluded.humidity,
                pressure    = excluded.pressure
            """,
            (temperature, humidity, pressure, ts),
        )
        await db.commit()
    invalidate_cache()
    return cursor.lastrowid


async def remove_readings_in_range(from_ts: str, to_ts: str) -> int:
    """Delete every reading between two timestamps (inclusive)."""
    start, end = clock.normalise_ts(from_ts), clock.normalise_ts(to_ts)
    async with _write_lock, acquire() as db:
        cursor = await db.execute(
            "DELETE FROM weather_readings WHERE timestamp >= ? AND timestamp <= ?",
            (start, end),
        )
        deleted = cursor.rowcount
        await db.commit()
    invalidate_cache()
    log.info("cleanup: removed %d reading(s) between %s and %s", deleted, start, end)
    return deleted


async def remove_off_grid_readings() -> int:
    """Delete readings that are not on the expected minute grid, plus duplicates.

    Duplicates cannot normally exist any more (the timestamp is unique), but
    the de-duplication is kept so the endpoint still repairs a database that
    predates that index.
    """
    async with _write_lock, acquire() as db:
        cursor = await db.execute(_DEDUPE)
        total = cursor.rowcount
        cursor = await db.execute(
            """
            DELETE FROM weather_readings
            WHERE CAST(strftime('%M', timestamp) AS INTEGER) % ? != 0
               OR CAST(strftime('%S', timestamp) AS INTEGER) != 0
            """,
            (READING_INTERVAL_MINUTES,),
        )
        total += cursor.rowcount
        await db.commit()
    invalidate_cache()
    log.info("cleanup: removed %d off-grid or duplicate reading(s)", total)
    return total


# ── Reads ──────────────────────────────────────────────────────────────────


async def get_current() -> dict | None:
    """The newest reading by timestamp.

    Ordered by timestamp rather than id: a backfill inserts old readings with
    fresh ids, and ordering by id would make one of those the "current" one.
    """
    return await _fetch_one(
        f"SELECT timestamp, {', '.join(METRICS)} FROM weather_readings "
        f"ORDER BY timestamp DESC LIMIT 1"
    )


def _cutoff_clause(period: str, reference: datetime | None = None) -> tuple[str, list[str]]:
    """Return a ``WHERE`` fragment and params bounding ``period``."""
    cutoff = clock.period_cutoff(period, reference)
    if cutoff is None:
        return "", []
    return "WHERE timestamp >= ?", [clock.fmt_ts(cutoff)]


@dataclass(frozen=True)
class HistorySeries:
    """Readings for a period, possibly averaged into fixed-width buckets."""

    readings: list[dict]
    #: Spacing between points in seconds — the raw interval, or the bucket width.
    interval_seconds: int
    #: True when points are bucket averages rather than individual readings.
    bucketed: bool

    def as_dict(self) -> dict:
        return {
            "readings": self.readings,
            "interval_seconds": self.interval_seconds,
            "bucketed": self.bucketed,
        }


async def _period_span(period: str, reference: datetime | None = None) -> timedelta:
    """How much wall-clock time ``period`` covers, given the stored data."""
    ref = reference if reference is not None else clock.now()
    cutoff = clock.period_cutoff(period, ref)
    if cutoff is not None:
        return ref - cutoff
    row = await _fetch_one("SELECT MIN(timestamp) AS first FROM weather_readings")
    if not row or not row["first"]:
        return timedelta(0)
    first = datetime.strptime(row["first"], clock.TS_FORMAT)
    return max(ref.replace(tzinfo=None) - first, timedelta(0))


def _choose_bucket_minutes(span: timedelta) -> int:
    """Smallest ladder step that keeps a span under the chart point budget."""
    minutes = max(span.total_seconds() / 60, 1)
    for step in _BUCKET_LADDER:
        if minutes / step <= TARGET_CHART_POINTS:
            return step
    return _BUCKET_LADDER[-1]


def _bucketed_sql(where: str, bucket_minutes: int) -> str:
    """Average each metric over fixed-width buckets, keeping min/max as a band."""
    aggregates = ",\n        ".join(
        f"ROUND(AVG({m}), 2) AS {m}, "
        f"ROUND(MIN({m}), 1) AS {m}_min, "
        f"ROUND(MAX({m}), 1) AS {m}_max"
        for m in METRICS
    )
    # strftime('%s') reads the stored string as UTC. That is the wrong instant
    # but a consistent one, which is all bucketing needs.
    return f"""
        SELECT
            strftime('%Y-%m-%d %H:%M:00',
                     (CAST(strftime('%s', timestamp) AS INTEGER)
                      / (? * 60)) * (? * 60), 'unixepoch') AS timestamp,
            {aggregates},
            COUNT(*) AS samples
        FROM weather_readings
        {where}
        GROUP BY CAST(strftime('%s', timestamp) AS INTEGER) / (? * 60)
        ORDER BY timestamp ASC
    """


@cached
async def get_history_series(
    period: str = "24h", reference: datetime | None = None
) -> HistorySeries:
    """Readings for ``period``, downsampled when the raw series is too large.

    Short periods come back untouched; longer ones are averaged into buckets
    so a two-year chart is a few hundred kilobytes rather than tens of
    megabytes. ``interval_seconds`` tells the client how far apart points are
    so it can tell a bucket boundary from a real outage.
    """
    where, params = _cutoff_clause(period, reference)
    span = await _period_span(period, reference)
    bucket = _choose_bucket_minutes(span)

    if bucket <= READING_INTERVAL_MINUTES:
        rows = await _fetch_all(
            f"SELECT timestamp, {', '.join(METRICS)} FROM weather_readings "
            f"{where} ORDER BY timestamp ASC",
            params,
        )
        return HistorySeries(rows, READING_INTERVAL_MINUTES * 60, bucketed=False)

    rows = await _fetch_all(
        _bucketed_sql(where, bucket), [bucket, bucket, *params, bucket]
    )
    return HistorySeries(rows, bucket * 60, bucketed=True)


async def get_history(period: str = "24h") -> list[dict]:
    """Raw readings for ``period``, with no downsampling."""
    where, params = _cutoff_clause(period)
    return await _fetch_all(
        f"SELECT timestamp, {', '.join(METRICS)} FROM weather_readings "
        f"{where} ORDER BY timestamp ASC",
        params,
    )


def _round_or_none(value: float | None, digits: int = 1) -> float | None:
    return round(value, digits) if value is not None else None


@cached
async def get_stats(period: str = "24h") -> dict:
    """Min/max/avg for each metric over ``period``."""
    where, params = _cutoff_clause(period)
    aggregates = ", ".join(
        f"MIN({m}) AS {m}_min, MAX({m}) AS {m}_max, AVG({m}) AS {m}_avg"
        for m in METRICS
    )
    row = await _fetch_one(
        f"SELECT {aggregates}, COUNT(*) AS count FROM weather_readings {where}", params
    )
    if not row or not row["count"]:
        return {"count": 0}

    stats: dict[str, Any] = {"count": row["count"]}
    for metric in METRICS:
        stats[metric] = {
            key: _round_or_none(row[f"{metric}_{key}"]) for key in ("min", "max", "avg")
        }
    return stats


@cached
async def get_extremes_with_times(period: str = "today") -> dict | None:
    """Min and max of every metric with the time each occurred.

    One round trip: each extreme is a ``LIMIT 1`` sub-select unioned together.
    Returns ``None`` when the period holds no readings.
    """
    where, params = _cutoff_clause(period)
    selects, query_params = [], []
    for metric in METRICS:
        for kind, order in (("min", "ASC"), ("max", "DESC")):
            # Each branch needs its own parentheses: SQLite rejects a bare
            # ORDER BY/LIMIT inside a compound SELECT.
            selects.append(
                f"SELECT * FROM (SELECT '{metric}' AS metric, '{kind}' AS kind, "
                f"{metric} AS value, timestamp FROM weather_readings {where} "
                f"ORDER BY {metric} {order}, timestamp ASC LIMIT 1)"
            )
            query_params.extend(params)

    rows = await _fetch_all(" UNION ALL ".join(selects), query_params)
    if not rows:
        return None

    result: dict[str, Any] = {metric: {} for metric in METRICS}
    for row in rows:
        result[row["metric"]][row["kind"]] = {
            "value": round(row["value"], 1),
            "timestamp": row["timestamp"],
        }
    return result


async def get_pressure_trend(hours: int = TREND_WINDOW_HOURS) -> dict | None:
    """Pressure change over ``hours``, with acceleration and consistency.

    Both ends of the comparison are medians — the last half hour against a
    half hour centred on ``hours`` ago — so one jittery sample cannot flip
    the direction, and both are corrected for the station's daily pressure
    cycle (see :func:`get_pressure_cycle`). Without that correction the
    change is dominated by the time of day rather than by the weather.
    """
    recent = await _fetch_all(
        "SELECT timestamp, pressure FROM weather_readings "
        f"ORDER BY timestamp DESC LIMIT {SMOOTHING_READINGS * 2}"
    )
    if not recent:
        return None

    cycle = await get_pressure_cycle()
    corrected = [_detide(row, cycle) for row in recent]

    current_p = _median(corrected[:SMOOTHING_READINGS])
    prev_p = await _pressure_at(hours, cycle)
    if prev_p is None:
        first = await _fetch_one(
            "SELECT timestamp, pressure FROM weather_readings ORDER BY timestamp ASC LIMIT 1"
        )
        prev_p = _detide(first, cycle) if first else current_p

    delta = round(current_p - prev_p, 1)
    direction = _direction(delta)

    acceleration = None
    older_p = await _pressure_at(hours * 2, cycle)
    if older_p is not None:
        acceleration = _acceleration(direction, _direction(round(prev_p - older_p, 1)))

    return {
        "current": round(current_p, 1),
        "previous": round(prev_p, 1),
        "delta": delta,
        "direction": direction,
        "acceleration": acceleration,
        "consistency": _consistency(corrected, direction),
        "hours": hours,
        "detided": bool(cycle),
    }


@cached
async def get_pressure_cycle() -> dict[int, float]:
    """The station's own mean daily pressure cycle, per half-hour slot.

    A BME280 reduces its reading to sea level using temperature, so a sensor
    that bakes in the afternoon sun writes its own day/night rhythm into the
    "pressure" it reports. Measured over this station's history that swing is
    around 3 hPa — several times the real atmospheric tide at this latitude,
    and far larger than the change any forecast rule is looking for. So the
    mean offset of each slot is learned from complete days and subtracted
    before any trend is taken.

    Returns an empty mapping until :data:`CYCLE_MIN_DAYS` complete days exist,
    in which case no correction is applied at all — a young database has no
    cycle to learn from, and half a cycle is worse than none.
    """
    cutoff = clock.fmt_ts(clock.now() - timedelta(days=CYCLE_LEARN_DAYS))
    rows = await _fetch_all(
        """
        WITH recent AS (
            SELECT substr(timestamp, 1, 10) AS day,
                   CAST(substr(timestamp, 12, 2) AS INTEGER) * 2
                     + (CAST(substr(timestamp, 15, 2) AS INTEGER) / 30) AS slot,
                   pressure
            FROM weather_readings
            WHERE timestamp >= ?
        ),
        complete AS (
            SELECT day, AVG(pressure) AS mean_pressure
            FROM recent
            GROUP BY day
            HAVING COUNT(*) >= ?
        )
        SELECT r.slot AS slot,
               AVG(r.pressure - c.mean_pressure) AS offset,
               COUNT(DISTINCT r.day) AS days
        FROM recent r
        JOIN complete c ON c.day = r.day
        GROUP BY r.slot
        """,
        (cutoff, CYCLE_MIN_READINGS_PER_DAY),
    )
    if not rows or max(row["days"] for row in rows) < CYCLE_MIN_DAYS:
        return {}
    return {int(row["slot"]): round(row["offset"], 2) for row in rows}


def _slot_of(timestamp: str) -> int:
    """The half-hour slot of the day a timestamp falls in (0-47)."""
    return int(timestamp[11:13]) * 2 + int(timestamp[14:16]) // 30


def _detide(row: Any, cycle: dict[int, float]) -> float:
    """A reading's pressure with the station's daily cycle removed."""
    return row["pressure"] - cycle.get(_slot_of(row["timestamp"]), 0.0)


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


async def _pressure_at(hours: float, cycle: dict[int, float]) -> float | None:
    """Median corrected pressure in a window centred ``hours`` ago."""
    centre = clock.now() - timedelta(hours=hours)
    half = timedelta(minutes=SMOOTHING_WINDOW_MINUTES / 2)
    rows = await _fetch_all(
        "SELECT timestamp, pressure FROM weather_readings "
        "WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp ASC",
        (clock.fmt_ts(centre - half), clock.fmt_ts(centre + half)),
    )
    if not rows:
        return None
    return _median([_detide(row, cycle) for row in rows])


def _direction(delta: float) -> str:
    if delta > 0.5:
        return "rising"
    return "falling" if delta < -0.5 else "steady"


def _acceleration(current: str, previous: str) -> str | None:
    """How this window's trend compares with the one before it."""
    if current == "steady":
        return "ending" if previous != "steady" else None
    if previous == "steady":
        return "starting"
    return "sustained" if previous == current else "reversing"


def _consistency(pressures: Iterable[float], direction: str) -> float:
    """Fraction of recent steps that agree with ``direction``.

    ``pressures`` is newest-first, so each step is newer minus older.
    """
    values = list(pressures)
    agree = 0
    for newer, older in itertools.pairwise(values):
        step = newer - older
        if (
            (direction == "rising" and step > -0.1)
            or (direction == "falling" and step < 0.1)
            or (direction == "steady" and abs(step) < 0.3)
        ):
            agree += 1
    return round(agree / max(len(values) - 1, 1), 2)


async def get_recent_trend(column: str, hours: int = 3) -> dict | None:
    """Change in ``column`` over the last ``hours``.

    Both ends are medians over :data:`SMOOTHING_WINDOW_MINUTES`, like the
    pressure trend: a single noisy sample at either end would otherwise move
    the delta across a forecast threshold on its own.
    """
    if column not in METRICS:
        raise ValueError(f"unknown metric: {column!r}")

    recent = await _fetch_all(
        f"SELECT {column} AS value FROM weather_readings "
        f"ORDER BY timestamp DESC LIMIT {SMOOTHING_READINGS}"
    )
    if not recent:
        return None

    current_val = round(_median([row["value"] for row in recent]), 1)
    earlier = await _median_at(column, hours)
    prev_val = round(earlier, 1) if earlier is not None else current_val
    return {
        "current": current_val,
        "previous": prev_val,
        "delta": round(current_val - prev_val, 1),
    }


async def _median_at(column: str, hours: float) -> float | None:
    """Median of ``column`` in a smoothing window centred ``hours`` ago."""
    centre = clock.now() - timedelta(hours=hours)
    half = timedelta(minutes=SMOOTHING_WINDOW_MINUTES / 2)
    rows = await _fetch_all(
        f"SELECT {column} AS value FROM weather_readings "
        f"WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp ASC",
        (clock.fmt_ts(centre - half), clock.fmt_ts(centre + half)),
    )
    if not rows:
        return None
    return _median([row["value"] for row in rows])


async def get_reading_ago(hours: int = 24, tolerance_hours: float = 2.0) -> dict | None:
    """The reading closest to ``hours`` ago, or ``None`` if the data has a hole.

    Without the tolerance an outage would silently compare "now" against a
    reading days old and label it "vs 24h ago".
    """
    target = clock.now() - timedelta(hours=hours)
    window = timedelta(hours=tolerance_hours)
    return await _fetch_one(
        f"""
        SELECT timestamp, {', '.join(METRICS)} FROM weather_readings
        WHERE timestamp BETWEEN ? AND ?
        ORDER BY ABS(strftime('%s', timestamp) - strftime('%s', ?)) ASC
        LIMIT 1
        """,
        (clock.fmt_ts(target - window), clock.fmt_ts(target + window), clock.fmt_ts(target)),
    )


@cached
async def get_daily_summaries(months: int = 3) -> list[dict]:
    """Per-day min/max/avg temperature and humidity over the last ``months``."""
    cutoff = clock.fmt_ts(clock.months_ago(months))
    return await _fetch_all(
        """
        SELECT
            substr(timestamp, 1, 10) AS day,
            ROUND(MIN(temperature), 1) AS temp_min,
            ROUND(MAX(temperature), 1) AS temp_max,
            ROUND(AVG(temperature), 1) AS temp_avg,
            ROUND(MIN(humidity), 1) AS hum_min,
            ROUND(MAX(humidity), 1) AS hum_max,
            ROUND(AVG(humidity), 1) AS hum_avg
        FROM weather_readings
        WHERE timestamp >= ?
        GROUP BY day
        ORDER BY day ASC
        """,
        (cutoff,),
    )


def _midnight_today() -> str:
    """Upper bound excluding today, so a partial day cannot claim a record."""
    return clock.fmt_ts(clock.start_of_day(clock.now()))


@cached
async def get_daily_extremes() -> dict | None:
    """Days with the highest and lowest average temperature and humidity.

    Complete days only — today is still accumulating and would otherwise
    take the record every morning.
    """
    row = await _fetch_one(
        """
        WITH daily AS (
            SELECT substr(timestamp, 1, 10) AS day,
                   AVG(temperature) AS temp_avg,
                   AVG(humidity) AS hum_avg
            FROM weather_readings
            WHERE timestamp < ?
            GROUP BY day
        )
        SELECT
            (SELECT day      FROM daily ORDER BY temp_avg DESC LIMIT 1) AS hottest_avg_day,
            (SELECT temp_avg FROM daily ORDER BY temp_avg DESC LIMIT 1) AS hottest_avg,
            (SELECT day      FROM daily ORDER BY temp_avg ASC  LIMIT 1) AS coldest_avg_day,
            (SELECT temp_avg FROM daily ORDER BY temp_avg ASC  LIMIT 1) AS coldest_avg,
            (SELECT day      FROM daily ORDER BY hum_avg  DESC LIMIT 1) AS most_humid_avg_day,
            (SELECT hum_avg  FROM daily ORDER BY hum_avg  DESC LIMIT 1) AS most_humid_avg,
            (SELECT day      FROM daily ORDER BY hum_avg  ASC  LIMIT 1) AS least_humid_avg_day,
            (SELECT hum_avg  FROM daily ORDER BY hum_avg  ASC  LIMIT 1) AS least_humid_avg
        """,
        (_midnight_today(),),
    )
    # The outer SELECT always yields a row; an empty table just fills it
    # with NULLs, so test the payload rather than the row.
    return row if row and row["hottest_avg_day"] else None


# Timestamps are fixed-width ``YYYY-MM-DD HH:MM:SS`` by construction, so the
# year/month/day keys below are taken with substr() rather than strftime() or
# date(). Same results, measurably less work per row on a large archive.
_CLIMATE_AGGREGATES = """
    ROUND(AVG(temperature), 1) AS temp_avg,
    ROUND(MIN(temperature), 1) AS temp_min,
    ROUND(MAX(temperature), 1) AS temp_max,
    ROUND(AVG(humidity), 1) AS hum_avg,
    COUNT(*) AS readings
"""

_EMPTY_MONTH = {
    "temp_avg": None, "temp_min": None, "temp_max": None, "hum_avg": None, "readings": 0,
}


def _fill_year(by_month: dict[int, dict]) -> list[dict]:
    """Twelve entries, with placeholders for months that have no readings."""
    return [by_month.get(m, {"month": m, **_EMPTY_MONTH}) for m in range(1, 13)]


@cached
async def get_climate_stats() -> dict:
    """Monthly and yearly temperature aggregates for the climate chart.

    Complete days only, for the same reason as :func:`get_daily_extremes`.
    """
    cutoff = (_midnight_today(),)

    monthly_rows = await _fetch_all(
        f"""
        SELECT CAST(substr(timestamp, 6, 2) AS INTEGER) AS month, {_CLIMATE_AGGREGATES}
        FROM weather_readings WHERE timestamp < ? GROUP BY month ORDER BY month
        """,
        cutoff,
    )
    per_year_rows = await _fetch_all(
        f"""
        SELECT CAST(substr(timestamp, 1, 4) AS INTEGER) AS year,
               CAST(substr(timestamp, 6, 2) AS INTEGER) AS month, {_CLIMATE_AGGREGATES}
        FROM weather_readings WHERE timestamp < ? GROUP BY year, month ORDER BY year, month
        """,
        cutoff,
    )
    yearly = await _fetch_all(
        f"""
        SELECT CAST(substr(timestamp, 1, 4) AS INTEGER) AS year, {_CLIMATE_AGGREGATES}
        FROM weather_readings WHERE timestamp < ? GROUP BY year ORDER BY year
        """,
        cutoff,
    )

    grouped: dict[int, dict[int, dict]] = {}
    for row in per_year_rows:
        grouped.setdefault(row["year"], {})[row["month"]] = row

    return {
        "monthly_all": _fill_year({row["month"]: row for row in monthly_rows}),
        "years": sorted(grouped),
        "monthly_by_year": {str(y): _fill_year(months) for y, months in sorted(grouped.items())},
        "yearly": yearly,
    }
