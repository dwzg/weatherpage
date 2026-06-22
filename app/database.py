import os
import aiosqlite
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

DB_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DB_DIR / "weather.db"
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Berlin"))


def _now() -> datetime:
    """Current time in the configured timezone."""
    return datetime.now(tz=TIMEZONE)


def _fmt_cutoff(dt: datetime) -> str:
    """Format a datetime to match the DB timestamp format (space-separated, from HA)."""
    return dt.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


async def get_db() -> aiosqlite.Connection:
    """Get a database connection. Caller must close it."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    return db


async def init_db() -> None:
    """Create tables if they don't exist."""
    db = await get_db()
    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS weather_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                temperature REAL NOT NULL,
                humidity REAL NOT NULL,
                pressure REAL NOT NULL,
                timestamp TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_timestamp
            ON weather_readings(timestamp)
        """)
        await db.commit()
    finally:
        await db.close()


async def insert_reading(temperature: float, humidity: float, pressure: float, timestamp: str) -> int:
    """Insert a weather reading. Returns the new row id."""
    # Normalize to space-separated format matching HA's output
    ts = timestamp.replace("T", " ")
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO weather_readings (temperature, humidity, pressure, timestamp) VALUES (?, ?, ?, ?)",
            (temperature, humidity, pressure, ts),
        )
        await db.commit()
        return cursor.lastrowid
    finally:
        await db.close()


async def get_current() -> dict | None:
    """Get the most recent reading."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM weather_readings ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


def _period_to_cutoff(period: str) -> datetime | None:
    """Convert a period string to a cutoff datetime in local time. Returns None for 'all'."""
    now = _now()
    if period == "3h":
        return now - timedelta(hours=3)
    elif period == "24h":
        return now - timedelta(hours=24)
    elif period == "7d":
        return now - timedelta(days=7)
    elif period == "30d":
        return now - timedelta(days=30)
    elif period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        return None  # 'all' or unknown


async def get_history(period: str = "24h") -> list[dict]:
    """Get all readings within the given period ('24h', '7d', '30d', 'all')."""
    db = await get_db()
    try:
        cutoff = _period_to_cutoff(period)
        if cutoff:
            cursor = await db.execute(
                "SELECT * FROM weather_readings WHERE timestamp >= ? ORDER BY timestamp ASC",
                (_fmt_cutoff(cutoff),),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM weather_readings ORDER BY timestamp ASC"
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_stats(period: str = "24h") -> dict:
    """Get min/max/avg stats for each metric over the given period."""
    db = await get_db()
    try:
        cutoff = _period_to_cutoff(period)
        if cutoff:
            cursor = await db.execute(
                """
                SELECT
                    MIN(temperature) as temp_min, MAX(temperature) as temp_max,
                    AVG(temperature) as temp_avg,
                    MIN(humidity) as hum_min, MAX(humidity) as hum_max,
                    AVG(humidity) as hum_avg,
                    MIN(pressure) as pres_min, MAX(pressure) as pres_max,
                    AVG(pressure) as pres_avg,
                    COUNT(*) as count
                FROM weather_readings
                WHERE timestamp >= ?
                """,
                (_fmt_cutoff(cutoff),),
            )
        else:
            cursor = await db.execute(
                """
                SELECT
                    MIN(temperature) as temp_min, MAX(temperature) as temp_max,
                    AVG(temperature) as temp_avg,
                    MIN(humidity) as hum_min, MAX(humidity) as hum_max,
                    AVG(humidity) as hum_avg,
                    MIN(pressure) as pres_min, MAX(pressure) as pres_max,
                    AVG(pressure) as pres_avg,
                    COUNT(*) as count
                FROM weather_readings
                """
            )
        row = await cursor.fetchone()
        r = dict(row)
        if r["count"] == 0:
            return {"count": 0}

        def round_or_none(val):
            return round(val, 1) if val is not None else None

        return {
            "count": r["count"],
            "temperature": {
                "min": round_or_none(r["temp_min"]),
                "max": round_or_none(r["temp_max"]),
                "avg": round_or_none(r["temp_avg"]),
            },
            "humidity": {
                "min": round_or_none(r["hum_min"]),
                "max": round_or_none(r["hum_max"]),
                "avg": round_or_none(r["hum_avg"]),
            },
            "pressure": {
                "min": round_or_none(r["pres_min"]),
                "max": round_or_none(r["pres_max"]),
                "avg": round_or_none(r["pres_avg"]),
            },
        }
    finally:
        await db.close()


async def get_extremes_with_times(period: str = "today") -> dict:
    """Get min/max values with their timestamps for the given period."""
    db = await get_db()
    try:
        cutoff = _period_to_cutoff(period)
        if cutoff is None:
            cutoff_str = "2000-01-01T00:00:00"  # far past for 'all'
        else:
            cutoff_str = _fmt_cutoff(cutoff)

        result = {"count": 0, "temperature": {}, "humidity": {}, "pressure": {}}

        # Min temperature with timestamp
        cursor = await db.execute(
            "SELECT temperature, timestamp FROM weather_readings WHERE timestamp >= ? ORDER BY temperature ASC LIMIT 1",
            (cutoff_str,),
        )
        row = await cursor.fetchone()
        if row:
            result["temperature"]["min"] = {"value": round(row[0], 1), "timestamp": row[1]}

        # Max temperature with timestamp
        cursor = await db.execute(
            "SELECT temperature, timestamp FROM weather_readings WHERE timestamp >= ? ORDER BY temperature DESC LIMIT 1",
            (cutoff_str,),
        )
        row = await cursor.fetchone()
        if row:
            result["temperature"]["max"] = {"value": round(row[0], 1), "timestamp": row[1]}

        # Min humidity
        cursor = await db.execute(
            "SELECT humidity, timestamp FROM weather_readings WHERE timestamp >= ? ORDER BY humidity ASC LIMIT 1",
            (cutoff_str,),
        )
        row = await cursor.fetchone()
        if row:
            result["humidity"]["min"] = {"value": round(row[0], 1), "timestamp": row[1]}

        # Max humidity
        cursor = await db.execute(
            "SELECT humidity, timestamp FROM weather_readings WHERE timestamp >= ? ORDER BY humidity DESC LIMIT 1",
            (cutoff_str,),
        )
        row = await cursor.fetchone()
        if row:
            result["humidity"]["max"] = {"value": round(row[0], 1), "timestamp": row[1]}

        # Min pressure
        cursor = await db.execute(
            "SELECT pressure, timestamp FROM weather_readings WHERE timestamp >= ? ORDER BY pressure ASC LIMIT 1",
            (cutoff_str,),
        )
        row = await cursor.fetchone()
        if row:
            result["pressure"]["min"] = {"value": round(row[0], 1), "timestamp": row[1]}

        # Max pressure
        cursor = await db.execute(
            "SELECT pressure, timestamp FROM weather_readings WHERE timestamp >= ? ORDER BY pressure DESC LIMIT 1",
            (cutoff_str,),
        )
        row = await cursor.fetchone()
        if row:
            result["pressure"]["max"] = {"value": round(row[0], 1), "timestamp": row[1]}

        if result["temperature"]:
            result["count"] = 1  # signal we have data
        return result
    finally:
        await db.close()


async def get_pressure_trend(hours: int = 6) -> dict | None:
    """Return pressure trend: current, previous (N hours ago), delta, direction."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT pressure, timestamp FROM weather_readings ORDER BY id DESC LIMIT 1"
        )
        latest = await cursor.fetchone()
        if not latest:
            return None

        cutoff = _fmt_cutoff(_now() - timedelta(hours=hours))
        cursor = await db.execute(
            "SELECT pressure FROM weather_readings WHERE timestamp >= ? ORDER BY id ASC LIMIT 1",
            (cutoff,),
        )
        earlier = await cursor.fetchone()
        current_p = round(latest[0], 1)
        if earlier:
            prev_p = round(earlier[0], 1)
        else:
            # No reading from N hours ago, use oldest available
            cursor = await db.execute("SELECT pressure FROM weather_readings ORDER BY id ASC LIMIT 1")
            first = await cursor.fetchone()
            if first and first[0] != latest[0]:
                prev_p = round(first[0], 1)
            else:
                prev_p = current_p

        delta = round(current_p - prev_p, 1)
        if delta > 0.5:
            direction = "rising"
        elif delta < -0.5:
            direction = "falling"
        else:
            direction = "steady"

        return {"current": current_p, "previous": prev_p, "delta": delta, "direction": direction}
    finally:
        await db.close()


async def get_daily_summaries(months: int = 3) -> list[dict]:
    """Return daily min/max/avg for each day in the last N months."""
    db = await get_db()
    try:
        cutoff = _fmt_cutoff(_now() - timedelta(days=months * 30))
        cursor = await db.execute(
            """
            SELECT
                date(timestamp) as day,
                MIN(temperature) as temp_min,
                MAX(temperature) as temp_max,
                ROUND(AVG(temperature), 1) as temp_avg,
                MIN(humidity) as hum_min,
                MAX(humidity) as hum_max,
                ROUND(AVG(humidity), 1) as hum_avg
            FROM weather_readings
            WHERE timestamp >= ?
            GROUP BY date(timestamp)
            ORDER BY day ASC
            """,
            (cutoff,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_reading_ago(hours: int = 24) -> dict | None:
    """Get the reading closest to N hours ago."""
    db = await get_db()
    try:
        cutoff = _fmt_cutoff(_now() - timedelta(hours=hours))
        cursor = await db.execute(
            "SELECT * FROM weather_readings WHERE timestamp <= ? ORDER BY timestamp DESC LIMIT 1",
            (cutoff,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()
