import os
import aiosqlite
from datetime import datetime, timedelta
from pathlib import Path

DB_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DB_DIR / "weather.db"


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
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO weather_readings (temperature, humidity, pressure, timestamp) VALUES (?, ?, ?, ?)",
            (temperature, humidity, pressure, timestamp),
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
    """Convert a period string to a cutoff datetime. Returns None for 'all'."""
    now = datetime.utcnow()
    if period == "24h":
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
                (cutoff.isoformat(),),
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
                (cutoff.isoformat(),),
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
