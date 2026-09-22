"""All SQLite access.

Readings live in ``weather_readings``; ``daily_rollup`` holds one summary
row per day, maintained on every write, and the aggregates that span the
whole archive are built from those rather than from the readings. Connections
come from a small pool opened at startup (see :func:`connect` /
:func:`disconnect`) rather than being created per call, so a page render does
not pay a dozen connection setups.

Timestamps follow the convention documented in :mod:`app.clock`: naive
local-time strings compared lexicographically. Build every bound with
``clock.fmt_ts`` so comparisons stay on that path. Each row also carries the
UTC offset of the instant it was taken at, which is what tells the two passes
through the repeated autumn hour apart — see :data:`ORDER_OLDEST_FIRST`.
"""

from __future__ import annotations

import asyncio
import bisect
import itertools
import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
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

#: Chronological ordering, oldest first, and its reverse.
#:
#: The local timestamp alone is not a total order: during the autumn DST
#: fallback 02:00-02:59 happens twice and the two runs share every string.
#: Within one local timestamp the larger UTC offset is the earlier instant
#: (+02:00 before +01:00), so the offset breaks the tie. Order by these rather
#: than by ``timestamp`` alone, and never by ``id`` — a backfill inserts old
#: readings with fresh ids.
#: Both orderings mix directions, which no same-direction index can serve: an
#: index on ``(timestamp, utc_offset)`` read backwards gives ``timestamp DESC,
#: utc_offset DESC``, not this. Before ``idx_timestamp_desc`` existed, every
#: "latest reading" query therefore scanned the whole table and sorted it —
#: 35 ms for a ``LIMIT 1`` over 500 days, growing with the archive, on the one
#: path every page render and every poll goes through. The index below is
#: declared with exactly these directions, and serves the reverse by being
#: scanned backwards, so one index covers both constants.
ORDER_OLDEST_FIRST = "ORDER BY timestamp ASC, utc_offset DESC"
ORDER_NEWEST_FIRST = "ORDER BY timestamp DESC, utc_offset ASC"

#: Connections kept open. SQLite serialises writes anyway; a handful of
#: readers is plenty for a single-dashboard app and avoids reconnect cost.
POOL_SIZE = 4

#: Roughly how many points a chart should receive. Longer periods are
#: bucketed to stay near this, instead of shipping every 5-minute row.
TARGET_CHART_POINTS = 1500

#: How many raw readings one export page carries, and the ceiling a caller
#: may ask for. Nothing to do with TARGET_CHART_POINTS above: that is a
#: drawing budget, this is a transfer size. A year of 5-minute readings is
#: about 105k rows, so a full export is a handful of requests.
EXPORT_PAGE_SIZE = 10_000
EXPORT_MAX_PAGE_SIZE = 50_000

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

#: How much recent history the current pressure is ranked against, and the
#: hourly readings needed before that ranking means anything.
PERCENTILE_DAYS = 30
PERCENTILE_MIN_READINGS = 7 * 24
#: Fraction of a shorter window that must be populated for it to count.
PERCENTILE_COVERAGE = 0.6

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

# ``utc_offset`` is minutes east of UTC for the instant the reading was
# taken. The DEFAULT is never the right answer for a real reading and exists
# only so the column can be added NOT NULL to an existing table; the migration
# fills every row in immediately afterwards, and every insert supplies it.
#
# The index carries the directions of ORDER_NEWEST_FIRST verbatim — see the
# note beside those constants. It also serves the plain range scans the chart
# periods do (``WHERE timestamp >= ?``), because a range on the leading column
# does not care which way the index is sorted, which is why the old
# ``idx_timestamp`` is dropped by the migration rather than kept beside it.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS weather_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    temperature REAL NOT NULL,
    humidity REAL NOT NULL,
    pressure REAL NOT NULL,
    timestamp TEXT NOT NULL,
    utc_offset INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_DEDUPE = """
DELETE FROM weather_readings WHERE id NOT IN (
    SELECT MIN(id) FROM weather_readings GROUP BY timestamp, utc_offset
)
"""

#: The unique index ingestion upserts against. Both columns: a repeated local
#: hour holds two genuinely different readings, and collapsing them would
#: throw an hour of the archive away once a year.
_UNIQUE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_timestamp_unique "
    "ON weather_readings(timestamp, utc_offset)"
)

#: The index every read goes through. Created by the migration rather than by
#: _SCHEMA because it names ``utc_offset``, which an older database does not
#: have until ``_add_utc_offset`` has run.
_ORDER_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_timestamp_desc "
    "ON weather_readings(timestamp DESC, utc_offset ASC)"
)


# ── The prediction log ─────────────────────────────────────────────────────
#
# What the page actually said, hour by hour, so it can be held against what
# the weather actually did. Everything else about the models is scored at fit
# time, walk-forward, against a held-out past — which says the method works,
# not that the thing currently deployed has been right.
#
# This cannot be reconstructed after the fact, which is the whole reason for
# the table. The features are a pure function of the readings, so a replay
# could recompute them — but it would attribute every past hour to *today's*
# model, and the model is refitted weekly. A backfill changes the inputs a
# replay would see, too. Only a row written at the time records what was on
# the page at the time.
#
# ``(timestamp, utc_offset)`` is the key for the same reason it is on the
# readings: one repeated autumn hour holds two different hours of weather,
# and the rest of the codebase already treats that pair as the identity of a
# moment.
_PREDICTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS prediction_log (
    timestamp TEXT NOT NULL,
    utc_offset INTEGER NOT NULL,
    rain_probability REAL,
    sky_probability REAL,
    forecast TEXT,
    model_trained_at TEXT,
    logged_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (timestamp, utc_offset)
);
"""

#: Predictions are logged on the hour, because the observations they will be
#: scored against are hourly. A row per reading would be twelve times the
#: rows and not one extra scoreable hour.
PREDICTION_LOG_SUFFIX = ":00:00"


# ── The daily rollup ───────────────────────────────────────────────────────
#
# Every aggregate that spans the whole archive used to scan every reading.
# The cache hid that between writes, but the work grew with the archive and a
# write every five minutes threw the answers away again. A day of readings is
# immutable once it is past, so each day is summarised once into
# ``daily_rollup`` and the whole-archive aggregates read the summaries: a few
# hundred rows a year instead of a hundred thousand.
#
# Only whole-day questions can be answered this way, which is why the short
# chart periods still read the readings themselves — see _rollup_scope.

#: Per metric, per day. ``sum`` rather than ``avg`` so that rolling days up
#: into a month stays exact: a mean of daily means is not the mean.
_ROLLUP_FIELDS = ("sum", "min", "min_at", "max", "max_at")

_ROLLUP_COLUMNS: tuple[str, ...] = (
    "day",
    "readings",
    *(f"{metric}_{field}" for metric in METRICS for field in _ROLLUP_FIELDS),
)

_ROLLUP_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_rollup (
    day TEXT PRIMARY KEY,
    readings INTEGER NOT NULL,
    {columns}
);
""".format(
    columns=",\n    ".join(
        f"{metric}_sum REAL NOT NULL, "
        f"{metric}_min REAL NOT NULL, {metric}_min_at TEXT NOT NULL, "
        f"{metric}_max REAL NOT NULL, {metric}_max_at TEXT NOT NULL"
        for metric in METRICS
    )
)

_ROLLUP_INSERT = (
    f"INSERT INTO daily_rollup ({', '.join(_ROLLUP_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(_ROLLUP_COLUMNS))})"
)

def _day_of(timestamp: str) -> str:
    """The day a timestamp falls on. The format is fixed-width, so a prefix."""
    return timestamp[:10]


def _day_bounds(first_day: str, last_day: str) -> tuple[str, str]:
    """Timestamps bounding a span of days, inclusive at both ends.

    String literals rather than date arithmetic, and — unlike a ``substr()``
    of the timestamp — bounds the timestamp index can be used for.
    """
    return f"{first_day} 00:00:00", f"{last_day} 23:59:59"


@dataclass
class _DaySummary:
    """One day of readings, folded down to the row the rollup stores."""

    day: str
    readings: int = 0
    totals: dict[str, float] = field(default_factory=dict)
    lowest: dict[str, tuple[float, str]] = field(default_factory=dict)
    highest: dict[str, tuple[float, str]] = field(default_factory=dict)

    def absorb(self, row: Any) -> None:
        """Fold in one reading.

        Rows arrive oldest first and the comparisons are strict, so an extreme
        that recurs keeps the time it was first reached — the same tie-break
        the readings table answered records with.
        """
        self.readings += 1
        for metric in METRICS:
            value = row[metric]
            self.totals[metric] = self.totals.get(metric, 0.0) + value
            low = self.lowest.get(metric)
            if low is None or value < low[0]:
                self.lowest[metric] = (value, row["timestamp"])
            high = self.highest.get(metric)
            if high is None or value > high[0]:
                self.highest[metric] = (value, row["timestamp"])

    def as_row(self) -> tuple:
        values: list[Any] = [self.day, self.readings]
        for metric in METRICS:
            values.append(self.totals[metric])
            values.extend(self.lowest[metric])
            values.extend(self.highest[metric])
        return tuple(values)


async def _summarise_days(
    db: aiosqlite.Connection, where: str = "", params: Sequence[Any] = ()
) -> list[tuple]:
    """Summarise the readings matching ``where``, one row per day.

    The readings are streamed in chronological order and folded a day at a
    time rather than loaded: this runs over the whole table when the rollup
    is first built, and an archive does not need to fit in memory for that.
    """
    cursor = await db.execute(
        f"SELECT timestamp, {', '.join(METRICS)} FROM weather_readings "
        f"{where} {ORDER_OLDEST_FIRST}",
        params,
    )
    summaries: list[tuple] = []
    current: _DaySummary | None = None
    async for row in cursor:
        day = _day_of(row["timestamp"])
        if current is None or current.day != day:
            if current is not None:
                summaries.append(current.as_row())
            current = _DaySummary(day)
        current.absorb(row)
    if current is not None:
        summaries.append(current.as_row())
    return summaries


async def _refresh_rollup(
    db: aiosqlite.Connection, first_day: str | None = None, last_day: str | None = None
) -> int:
    """Recompute the rollup for a span of days, or for the whole archive.

    Recomputed from the readings rather than adjusted in place: a day is at
    most a few hundred rows, and an incremental update that drifts from the
    table it summarises is a bug that shows up months later as a wrong
    record. Call it inside the write lock, before the commit, so a reading
    and its summary land together. Returns the number of days written.
    """
    if first_day is None or last_day is None:
        await db.execute("DELETE FROM daily_rollup")
        rows = await _summarise_days(db)
    else:
        await db.execute(
            "DELETE FROM daily_rollup WHERE day >= ? AND day <= ?", (first_day, last_day)
        )
        start, end = _day_bounds(first_day, last_day)
        rows = await _summarise_days(
            db, "WHERE timestamp >= ? AND timestamp <= ?", (start, end)
        )
    await db.executemany(_ROLLUP_INSERT, rows)
    return len(rows)


def _rollup_scope(period: str, reference: datetime | None = None) -> tuple[str, list] | None:
    """A ``WHERE`` over ``daily_rollup`` covering ``period``, or ``None``.

    A per-day summary can only answer a question whose bounds fall on day
    boundaries. "all" and "today" do; "24h" and the rest cut a day in half,
    and are short enough that the index makes scanning the readings cheap
    anyway. ``None`` means "ask the readings table".
    """
    if period == "all":
        return "", []
    if period == "today":
        ref = reference if reference is not None else clock.now()
        return "WHERE day = ?", [clock.fmt_date(ref)]
    return None


async def _is_empty(db: aiosqlite.Connection, table: str) -> bool:
    """Whether ``table`` holds no rows. The name is a literal from this module."""
    cursor = await db.execute(f"SELECT 1 FROM {table} LIMIT 1")
    return await cursor.fetchone() is None


async def _rollup_needs_building(db: aiosqlite.Connection) -> bool:
    """Whether the rollup has to be built from scratch.

    True for a database from before the rollup existed — and also for one
    whose build was interrupted. ``executescript`` commits, so the empty
    table outlives the transaction that was meant to fill it, and asking
    whether the table exists would call that done.
    """
    return await _is_empty(db, "daily_rollup") and not await _is_empty(
        db, "weather_readings"
    )


async def _has_column(db: aiosqlite.Connection, table: str, column: str) -> bool:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return any(row["name"] == column for row in await cursor.fetchall())


async def _index_definition(db: aiosqlite.Connection, name: str) -> str | None:
    """The ``CREATE INDEX`` statement behind ``name``, or ``None``."""
    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
    )
    row = await cursor.fetchone()
    return row["sql"] if row else None


async def _add_utc_offset(db: aiosqlite.Connection) -> None:
    """Give an older table its ``utc_offset`` column and fill it in.

    Nothing recorded which pass through a repeated autumn hour an existing
    row belongs to, so every row is read as the first pass — see
    :func:`app.clock.resolve_offset`. The offset only changes at a DST
    boundary, so it is resolved once per distinct hour rather than per row.
    """
    if await _has_column(db, "weather_readings", "utc_offset"):
        return

    await db.execute(
        "ALTER TABLE weather_readings ADD COLUMN utc_offset INTEGER NOT NULL DEFAULT 0"
    )
    cursor = await db.execute(
        "SELECT DISTINCT substr(timestamp, 1, 13) AS hour FROM weather_readings"
    )
    hours = [row["hour"] for row in await cursor.fetchall()]
    await db.executemany(
        "UPDATE weather_readings SET utc_offset = ? WHERE substr(timestamp, 1, 13) = ?",
        [(clock.resolve_offset(f"{hour}:00:00"), hour) for hour in hours],
    )
    log.warning(
        "migration: added utc_offset to %d hour(s) of existing readings", len(hours)
    )


async def _migrate(db: aiosqlite.Connection) -> None:
    """Create the schema and make ``(timestamp, utc_offset)`` unique.

    A unique key lets ingestion upsert instead of appending duplicates, which
    is what the cleanup endpoint used to have to undo by hand. An existing
    database may already contain duplicates, so the index creation is
    attempted first and only falls back to de-duplicating (keeping the
    earliest row per key, exactly as cleanup always has) if it fails.

    A database from before the offset existed carries the same index name
    over ``timestamp`` alone, which would keep collapsing the repeated autumn
    hour into one row. ``CREATE INDEX IF NOT EXISTS`` would silently accept
    that index, so it is dropped by definition rather than by name.

    A database whose rollup is missing or unbuilt has it summarised here,
    once, over whatever archive it already holds.

    ``idx_timestamp`` is dropped: ``idx_timestamp_desc`` supersedes it, and
    an index nothing reads still costs a write on every insert.
    """
    await db.executescript(_SCHEMA)
    await db.executescript(_ROLLUP_SCHEMA)
    await db.executescript(_PREDICTION_SCHEMA)
    await _add_utc_offset(db)

    # Only now can this be built: it names utc_offset, which the step above
    # may have just added. It answers everything its single-column predecessor
    # did — including the range scans, since a range on the leading column is
    # indifferent to the index's direction — so that one is dropped rather
    # than left beside it: a redundant index is not free, it is rewritten on
    # every one of the ~288 inserts a day.
    await db.execute(_ORDER_INDEX)
    await db.execute("DROP INDEX IF EXISTS idx_timestamp")

    existing = await _index_definition(db, "idx_timestamp_unique")
    if existing is not None and "utc_offset" not in existing:
        await db.execute("DROP INDEX idx_timestamp_unique")

    try:
        await db.execute(_UNIQUE_INDEX)
    except aiosqlite.IntegrityError:
        cursor = await db.execute(_DEDUPE)
        log.warning(
            "migration: removed %d duplicate reading(s) to make timestamps unique",
            cursor.rowcount,
        )
        await db.execute(_UNIQUE_INDEX)

    # Last, so it summarises the readings that survived the steps above.
    if await _rollup_needs_building(db):
        days = await _refresh_rollup(db)
        log.warning("migration: summarised %d day(s) into the daily rollup", days)
    await db.commit()


async def init_db() -> None:
    """Backwards-compatible alias for :func:`connect`."""
    await connect()


# ── Writes ─────────────────────────────────────────────────────────────────


async def insert_reading(
    temperature: float,
    humidity: float,
    pressure: float,
    timestamp: str,
    utc_offset: int | None = None,
) -> int:
    """Store a reading, replacing any existing one for the same instant.

    Returns the row id. ``timestamp`` may use the ISO ``T`` separator; it is
    normalised to the stored format. ``utc_offset`` says which pass through a
    repeated autumn hour this is; left out, it is resolved from the timestamp
    against the arrival time, which is right for a live reading and falls back
    to the first pass for anything replayed.
    """
    ts = clock.normalise_ts(timestamp)
    if utc_offset is None:
        utc_offset = clock.resolve_offset(timestamp, arrival=clock.now())
    async with _write_lock, acquire() as db:
        cursor = await db.execute(
            """
            INSERT INTO weather_readings
                (temperature, humidity, pressure, timestamp, utc_offset)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(timestamp, utc_offset) DO UPDATE SET
                temperature = excluded.temperature,
                humidity    = excluded.humidity,
                pressure    = excluded.pressure
            """,
            (temperature, humidity, pressure, ts, utc_offset),
        )
        await _refresh_rollup(db, _day_of(ts), _day_of(ts))
        await db.commit()
    invalidate_cache()
    return cursor.lastrowid


async def insert_prediction(
    timestamp: str,
    utc_offset: int,
    rain_probability: float | None,
    sky_probability: float | None,
    forecast: str | None,
    model_trained_at: str | None,
) -> None:
    """Record what the page was showing at ``timestamp``.

    Upserts, so re-posting an hour's reading corrects its prediction rather
    than leaving the first attempt behind — the same contract ingestion has.

    Deliberately does **not** touch the daily rollup or the cache. Those
    summarise the readings, and nothing here changes a reading; the rule that
    every write path must refresh the rollup is about ``weather_readings``.
    """
    async with _write_lock, acquire() as db:
        await db.execute(
            """
            INSERT INTO prediction_log
                (timestamp, utc_offset, rain_probability, sky_probability,
                 forecast, model_trained_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(timestamp, utc_offset) DO UPDATE SET
                rain_probability = excluded.rain_probability,
                sky_probability  = excluded.sky_probability,
                forecast         = excluded.forecast,
                model_trained_at = excluded.model_trained_at,
                logged_at        = excluded.logged_at
            """,
            (clock.normalise_ts(timestamp), int(utc_offset), rain_probability,
             sky_probability, forecast, model_trained_at),
        )
        await db.commit()


async def export_predictions(
    after: tuple[str, int] | None = None,
    limit: int = EXPORT_PAGE_SIZE,
) -> list[dict]:
    """Logged predictions, oldest first, paged like the readings export."""
    where, params = ["1 = 1"], []
    if after is not None:
        timestamp, offset = clock.normalise_ts(after[0]), int(after[1])
        where.append("(timestamp > ? OR (timestamp = ? AND utc_offset < ?))")
        params += [timestamp, timestamp, offset]

    return await _fetch_all(
        "SELECT timestamp, utc_offset, rain_probability, sky_probability, "
        "forecast, model_trained_at FROM prediction_log "
        f"WHERE {' AND '.join(where)} {ORDER_OLDEST_FIRST} LIMIT ?",
        (*params, limit),
    )


async def count_predictions() -> int:
    """How many hours have been logged. Used to decide whether to score."""
    row = await _fetch_one("SELECT COUNT(*) AS n FROM prediction_log")
    return int(row["n"]) if row else 0


async def export_readings(
    after: tuple[str, int] | None = None,
    until: str | None = None,
    limit: int = EXPORT_PAGE_SIZE,
) -> list[dict]:
    """Raw readings, oldest first, one page at a time.

    This exists because :func:`get_history_series` must not be used for it.
    That one downsamples above :data:`TARGET_CHART_POINTS` — correct for a
    chart, wrong for anything that needs the readings themselves. The
    retraining job read it for months and got 3-hourly bucket averages, from
    which none of the features (30-minute medians, 6 and 12 hour deltas) can
    be built, so it produced zero samples and exited green.

    ``after`` is an exclusive cursor of ``(timestamp, utc_offset)`` rather
    than a plain timestamp, because a timestamp is not unique: both passes
    through the repeated autumn hour share one. The predicate mirrors
    :data:`ORDER_OLDEST_FIRST`, where the larger offset is the earlier
    instant within that hour.
    """
    where, params = ["1 = 1"], []
    if until is not None:
        where.append("timestamp <= ?")
        params.append(clock.normalise_ts(until))
    if after is not None:
        timestamp, offset = clock.normalise_ts(after[0]), int(after[1])
        where.append("(timestamp > ? OR (timestamp = ? AND utc_offset < ?))")
        params += [timestamp, timestamp, offset]

    return await _fetch_all(
        f"SELECT timestamp, utc_offset, {', '.join(METRICS)} FROM weather_readings "
        f"WHERE {' AND '.join(where)} {ORDER_OLDEST_FIRST} LIMIT ?",
        (*params, limit),
    )


async def remove_readings_in_range(from_ts: str, to_ts: str) -> int:
    """Delete every reading between two timestamps (inclusive)."""
    start, end = clock.normalise_ts(from_ts), clock.normalise_ts(to_ts)
    async with _write_lock, acquire() as db:
        cursor = await db.execute(
            "DELETE FROM weather_readings WHERE timestamp >= ? AND timestamp <= ?",
            (start, end),
        )
        deleted = cursor.rowcount
        await _refresh_rollup(db, _day_of(start), _day_of(end))
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
        # Which days were touched is not known here, so the whole rollup is
        # rebuilt. This runs from a maintenance endpoint, not on every write.
        await _refresh_rollup(db)
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
        f"{ORDER_NEWEST_FIRST} LIMIT 1"
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
    #: How many readings a complete bucket holds. The client cannot work this
    #: out for itself: once bucketed, ``interval_seconds`` is the bucket width
    #: and the 5-minute grid it was built from is no longer visible in the
    #: response. Without it, a bucket averaged from an outage is
    #: indistinguishable from a full one. 1 for an unbucketed series.
    expected_samples: int = 1

    def as_dict(self) -> dict:
        return {
            "readings": self.readings,
            "interval_seconds": self.interval_seconds,
            "bucketed": self.bucketed,
            "expected_samples": self.expected_samples,
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
    # but a consistent one, which is all bucketing needs. It also puts both
    # passes through a repeated autumn hour in the same bucket, which is what
    # a chart wants: they share one position on a wall-clock axis.
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
            f"{where} {ORDER_OLDEST_FIRST}",
            params,
        )
        return HistorySeries(rows, READING_INTERVAL_MINUTES * 60, bucketed=False)

    rows = await _fetch_all(
        _bucketed_sql(where, bucket), [bucket, bucket, *params, bucket]
    )
    return HistorySeries(
        rows, bucket * 60, bucketed=True,
        expected_samples=bucket // READING_INTERVAL_MINUTES,
    )


async def get_history(period: str = "24h") -> list[dict]:
    """Raw readings for ``period``, with no downsampling."""
    where, params = _cutoff_clause(period)
    return await _fetch_all(
        f"SELECT timestamp, {', '.join(METRICS)} FROM weather_readings "
        f"{where} {ORDER_OLDEST_FIRST}",
        params,
    )


def _round_or_none(value: float | None, digits: int = 1) -> float | None:
    return round(value, digits) if value is not None else None


@cached
async def get_stats(period: str = "24h") -> dict:
    """Min/max/avg for each metric over ``period``.

    A whole-day period is answered from the rollup, so "all" costs a row per
    day rather than a row per reading; the rest scan the readings behind the
    timestamp index. The two agree exactly — a mean is the summed values over
    the summed count either way.
    """
    scope = _rollup_scope(period)
    if scope is not None:
        rollup_where, rollup_params = scope
        aggregates = ", ".join(
            f"MIN({m}_min) AS {m}_min, MAX({m}_max) AS {m}_max, "
            f"SUM({m}_sum) / SUM(readings) AS {m}_avg"
            for m in METRICS
        )
        row = await _fetch_one(
            f"SELECT {aggregates}, SUM(readings) AS count FROM daily_rollup "
            f"{rollup_where}",
            rollup_params,
        )
    else:
        where, params = _cutoff_clause(period)
        aggregates = ", ".join(
            f"MIN({m}) AS {m}_min, MAX({m}) AS {m}_max, AVG({m}) AS {m}_avg"
            for m in METRICS
        )
        row = await _fetch_one(
            f"SELECT {aggregates}, COUNT(*) AS count FROM weather_readings {where}",
            params,
        )
    if not row or not row["count"]:
        return {"count": 0}

    stats: dict[str, Any] = {"count": row["count"]}
    for metric in METRICS:
        stats[metric] = {
            key: _round_or_none(row[f"{metric}_{key}"]) for key in ("min", "max", "avg")
        }
    return stats


#: The two extremes of a metric, and the direction each sorts in.
_EXTREME_KINDS = (("min", "ASC"), ("max", "DESC"))


def _extreme_columns(table: str, metric: str, kind: str) -> tuple[str, str]:
    """The value and time columns holding ``metric``'s ``kind`` in ``table``."""
    if table == "daily_rollup":
        return f"{metric}_{kind}", f"{metric}_{kind}_at"
    return metric, "timestamp"


@cached
async def get_extremes_with_times(period: str = "today") -> dict | None:
    """Min and max of every metric with the time each occurred.

    One round trip: each extreme is a ``LIMIT 1`` sub-select unioned together.
    A whole-day period reads the rollup, which already holds each day's
    extremes and when they happened, so the all-time records cost six lookups
    over a row per day. Returns ``None`` when the period holds no readings.
    """
    scope = _rollup_scope(period)
    if scope is not None:
        where, params = scope
        table, tiebreak = "daily_rollup", ""
    else:
        where, params = _cutoff_clause(period)
        # Within one local timestamp the larger offset is the earlier instant.
        table, tiebreak = "weather_readings", ", utc_offset DESC"

    selects, query_params = [], []
    for metric, (kind, order) in itertools.product(METRICS, _EXTREME_KINDS):
        value_col, time_col = _extreme_columns(table, metric, kind)
        # Each branch needs its own parentheses: SQLite rejects a bare
        # ORDER BY/LIMIT inside a compound SELECT. The tie-break is the
        # earliest time the extreme was reached, which is also what the
        # rollup recorded for each day.
        selects.append(
            f"SELECT * FROM (SELECT '{metric}' AS metric, '{kind}' AS kind, "
            f"{value_col} AS value, {time_col} AS timestamp FROM {table} {where} "
            f"ORDER BY {value_col} {order}, {time_col} ASC{tiebreak} LIMIT 1)"
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
        f"{ORDER_NEWEST_FIRST} LIMIT {SMOOTHING_READINGS * 2}"
    )
    if not recent:
        return None

    cycle = await get_pressure_cycle()
    corrected = [_detide(row, cycle) for row in recent]

    current_p = _median(corrected[:SMOOTHING_READINGS])
    prev_p = await _pressure_at(hours, cycle)
    if prev_p is None:
        first = await _fetch_one(
            f"SELECT timestamp, pressure FROM weather_readings {ORDER_OLDEST_FIRST} LIMIT 1"
        )
        prev_p = _detide(first, cycle) if first else current_p

    delta = round(current_p - prev_p, 1)
    direction = _direction(delta)

    return {
        "current": round(current_p, 1),
        "previous": round(prev_p, 1),
        "delta": delta,
        "direction": direction,
        "consistency": _consistency(corrected, direction),
        "hours": hours,
        "detided": bool(cycle),
    }


async def get_pressure_percentile(
    pressure: float, days: int = PERCENTILE_DAYS
) -> float | None:
    """Where ``pressure`` sits within the station's own recent range, 0-1.

    Scored against observed rainfall, *where* the pressure sits predicts rain
    at this station and which way it is moving does not. But a fixed
    threshold does not transfer between months — "below 1020 hPa" scored a
    critical success index of 0.05 in one month of the sample and 0.44 in
    another — because the level a barometer calls low drifts with the
    season. Ranking against the last :data:`PERCENTILE_DAYS` of this
    station's own readings is stable across the whole sample, and needs
    nothing from outside the box.

    Returns ``None`` until there is enough history to rank against.
    """
    window = await _recent_pressures(days)
    if len(window) < _percentile_minimum(days):
        return None
    return round(bisect.bisect_left(window, pressure) / len(window), 3)


def _percentile_minimum(days: int) -> int:
    """Hourly readings needed before a ranking window means anything.

    The smaller of a week's worth and most of the window: a rank against a
    handful of readings is meaningless, but demanding every hour would let a
    single afternoon's outage silently switch the ranking — and the nowcast
    that reads it — off for days.
    """
    return min(PERCENTILE_MIN_READINGS, int(days * 24 * PERCENTILE_COVERAGE))


@cached
async def _recent_pressures(days: int = PERCENTILE_DAYS) -> list[float]:
    """Sorted de-tided pressures, one per hour, over the ranking window."""
    cutoff = clock.fmt_ts(clock.now() - timedelta(days=days))
    rows = await _fetch_all(
        "SELECT timestamp, pressure FROM weather_readings "
        "WHERE timestamp >= ? AND substr(timestamp, 15, 2) = '00' "
        f"{ORDER_OLDEST_FIRST}",
        (cutoff,),
    )
    cycle = await get_pressure_cycle()
    return sorted(_detide(row, cycle) for row in rows)


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
        f"WHERE timestamp >= ? AND timestamp <= ? {ORDER_OLDEST_FIRST}",
        (clock.fmt_ts(centre - half), clock.fmt_ts(centre + half)),
    )
    if not rows:
        return None
    return _median([_detide(row, cycle) for row in rows])


def _direction(delta: float) -> str:
    if delta > 0.5:
        return "rising"
    return "falling" if delta < -0.5 else "steady"


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


async def get_extreme(column: str, hours: float, highest: bool = True) -> float | None:
    """The highest (or lowest) value of ``column`` over the last ``hours``.

    The nowcast reads the humidity peak of the last few hours: air that has
    been at saturation recently behaves differently from air that has just
    arrived there, and a single current reading cannot tell them apart.
    """
    if column not in METRICS:
        raise ValueError(f"unknown metric: {column!r}")

    cutoff = clock.fmt_ts(clock.now() - timedelta(hours=hours))
    row = await _fetch_one(
        f"SELECT {'MAX' if highest else 'MIN'}({column}) AS value "
        f"FROM weather_readings WHERE timestamp >= ?",
        (cutoff,),
    )
    return row["value"] if row and row["value"] is not None else None


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
        f"{ORDER_NEWEST_FIRST} LIMIT {SMOOTHING_READINGS}"
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
        f"WHERE timestamp >= ? AND timestamp <= ? {ORDER_OLDEST_FIRST}",
        (clock.fmt_ts(centre - half), clock.fmt_ts(centre + half)),
    )
    if not rows:
        return None
    return _median([row["value"] for row in rows])


async def get_reading_ago(hours: int = 24, tolerance_hours: float = 2.0) -> dict | None:
    """What the station read about ``hours`` ago, smoothed, with when.

    Every other delta on this page is a median over
    :data:`SMOOTHING_WINDOW_MINUTES` at both ends, so that one noisy sample
    cannot move it. This one — the "vs 24h ago" line on all three cards —
    took two raw instantaneous readings and subtracted them, which is the
    noisiest comparison on the page attached to the largest numbers.

    It also reports the gap it actually measured. With a tolerance of two
    hours the nearest readings can be 22 or 26 hours old, and near sunrise
    that is several degrees; labelling it "vs 24h ago" regardless is a claim
    the data does not support. ``hours`` comes back as the real distance so
    the caller can say what it compared.

    ``None`` when there is nothing inside the tolerance — an outage must not
    silently become a comparison against whatever is nearest.
    """
    target = clock.now() - timedelta(hours=hours)
    window = timedelta(hours=tolerance_hours)
    rows = await _fetch_all(
        f"""
        SELECT timestamp, {', '.join(METRICS)} FROM weather_readings
        WHERE timestamp BETWEEN ? AND ?
        ORDER BY ABS(strftime('%s', timestamp) - strftime('%s', ?)) ASC
        LIMIT {SMOOTHING_READINGS}
        """,
        (clock.fmt_ts(target - window), clock.fmt_ts(target + window),
         clock.fmt_ts(target)),
    )
    if not rows:
        return None

    # The readings nearest the target, medianed — the same smoothing the
    # trends use, over whichever readings the window actually holds.
    smoothed = {
        metric: round(_median([row[metric] for row in rows]), 1)
        for metric in METRICS
    }
    # Report the middle of what was used, not the middle of what was asked
    # for: those differ exactly when the archive has a hole here.
    stamps = sorted(row["timestamp"] for row in rows)
    middle = stamps[len(stamps) // 2]
    measured = (
        clock.now().replace(tzinfo=None) - datetime.strptime(middle, clock.TS_FORMAT)
    )
    return {
        **smoothed,
        "timestamp": middle,
        "hours": round(measured.total_seconds() / 3600, 1),
        "readings": len(rows),
    }


@cached
async def get_daily_summaries(months: int = 3) -> list[dict]:
    """Per-day min/max/avg temperature and humidity over the last ``months``."""
    cutoff = clock.fmt_date(clock.months_ago(months))
    return await _fetch_all(
        """
        SELECT
            day,
            ROUND(temperature_min, 1) AS temp_min,
            ROUND(temperature_max, 1) AS temp_max,
            ROUND(temperature_sum / readings, 1) AS temp_avg,
            ROUND(humidity_min, 1) AS hum_min,
            ROUND(humidity_max, 1) AS hum_max,
            ROUND(humidity_sum / readings, 1) AS hum_avg
        FROM daily_rollup
        WHERE day >= ?
        ORDER BY day ASC
        """,
        (cutoff,),
    )


def _today() -> str:
    """Upper bound excluding today, so a partial day cannot claim a record."""
    return clock.fmt_date(clock.now())


@cached
async def get_daily_extremes() -> dict | None:
    """Days with the highest and lowest average temperature and humidity.

    Complete days only — today is still accumulating and would otherwise
    take the record every morning.
    """
    row = await _fetch_one(
        """
        WITH daily AS (
            SELECT day,
                   temperature_sum / readings AS temp_avg,
                   humidity_sum / readings AS hum_avg
            FROM daily_rollup
            WHERE day < ?
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
        (_today(),),
    )
    # The outer SELECT always yields a row; an empty table just fills it
    # with NULLs, so test the payload rather than the row.
    return row if row and row["hottest_avg_day"] else None


# Day keys are fixed-width ``YYYY-MM-DD`` by construction, so the year and
# month below are taken with substr() rather than strftime() or date(). Same
# results, measurably less work per row.
#
# The means are summed values over summed readings rather than an average of
# the daily averages, which would weight a day with an outage in it the same
# as a complete one.
_CLIMATE_AGGREGATES = """
    ROUND(SUM(temperature_sum) / SUM(readings), 1) AS temp_avg,
    ROUND(MIN(temperature_min), 1) AS temp_min,
    ROUND(MAX(temperature_max), 1) AS temp_max,
    ROUND(SUM(humidity_sum) / SUM(readings), 1) AS hum_avg,
    SUM(readings) AS readings
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
    cutoff = (_today(),)

    monthly_rows = await _fetch_all(
        f"""
        SELECT CAST(substr(day, 6, 2) AS INTEGER) AS month, {_CLIMATE_AGGREGATES}
        FROM daily_rollup WHERE day < ? GROUP BY month ORDER BY month
        """,
        cutoff,
    )
    per_year_rows = await _fetch_all(
        f"""
        SELECT CAST(substr(day, 1, 4) AS INTEGER) AS year,
               CAST(substr(day, 6, 2) AS INTEGER) AS month, {_CLIMATE_AGGREGATES}
        FROM daily_rollup WHERE day < ? GROUP BY year, month ORDER BY year, month
        """,
        cutoff,
    )
    yearly = await _fetch_all(
        f"""
        SELECT CAST(substr(day, 1, 4) AS INTEGER) AS year, {_CLIMATE_AGGREGATES}
        FROM daily_rollup WHERE day < ? GROUP BY year ORDER BY year
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
