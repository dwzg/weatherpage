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


async def remove_readings_in_range(from_ts: str, to_ts: str) -> int:
    """Delete all readings between two timestamps, then remove duplicates."""
    db = await get_db()
    try:
        from_fmt = from_ts.replace("T", " ")
        to_fmt = to_ts.replace("T", " ")
        cursor = await db.execute(
            "DELETE FROM weather_readings WHERE timestamp >= ? AND timestamp <= ?",
            (from_fmt, to_fmt),
        )
        deleted = cursor.rowcount
        # Also remove any duplicates this may have exposed
        cursor = await db.execute("""
            DELETE FROM weather_readings WHERE id NOT IN (
                SELECT MIN(id) FROM weather_readings GROUP BY timestamp
            )
        """)
        deleted += cursor.rowcount
        await db.commit()
        return deleted
    finally:
        await db.close()


async def remove_off_grid_readings() -> int:
    """Delete readings that aren't on the 5-minute grid, plus duplicates.
    Returns the number of deleted rows."""
    db = await get_db()
    try:
        total = 0

        # Remove duplicates: keep lowest id for each timestamp
        cursor = await db.execute("""
            DELETE FROM weather_readings WHERE id NOT IN (
                SELECT MIN(id) FROM weather_readings GROUP BY timestamp
            )
        """)
        total += cursor.rowcount

        # Remove off-grid: minute not divisible by 5, or seconds != 0
        cursor = await db.execute("""
            DELETE FROM weather_readings
            WHERE CAST(strftime('%M', timestamp) AS INTEGER) % 5 != 0
               OR CAST(strftime('%S', timestamp) AS INTEGER) != 0
        """)
        total += cursor.rowcount
        await db.commit()
        return total
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
    """Return pressure trend with smoothed current, acceleration, and consistency.

    Uses median of last 6 readings (30 min) for current pressure to filter
    sensor jitter. Also compares 0-6h trend vs 6-12h trend for acceleration.
    """
    db = await get_db()
    try:
        # Smoothed current: median of last 6 readings (30 min)
        cursor = await db.execute(
            "SELECT pressure FROM weather_readings ORDER BY id DESC LIMIT 6"
        )
        recent = [r[0] for r in await cursor.fetchall()]
        if not recent:
            return None
        recent.sort()
        current_p = round(recent[len(recent) // 2], 1)  # median

        # Pressure N hours ago (single reading near the cutoff)
        cutoff = _fmt_cutoff(_now() - timedelta(hours=hours))
        cursor = await db.execute(
            "SELECT pressure FROM weather_readings WHERE timestamp >= ? ORDER BY id ASC LIMIT 1",
            (cutoff,),
        )
        earlier = await cursor.fetchone()
        if earlier:
            prev_p = round(earlier[0], 1)
        else:
            cursor = await db.execute("SELECT pressure FROM weather_readings ORDER BY id ASC LIMIT 1")
            first = await cursor.fetchone()
            prev_p = round(first[0], 1) if first else current_p

        delta = round(current_p - prev_p, 1)
        if delta > 0.5:
            direction = "rising"
        elif delta < -0.5:
            direction = "falling"
        else:
            direction = "steady"

        # Acceleration: compare 0-6h trend to 6-12h trend
        accel = None
        if hours == 6:
            cutoff_12h = _fmt_cutoff(_now() - timedelta(hours=12))
            cursor = await db.execute(
                "SELECT pressure FROM weather_readings WHERE timestamp >= ? ORDER BY id ASC LIMIT 1",
                (cutoff_12h,),
            )
            row_12h = await cursor.fetchone()
            if row_12h:
                older_p = round(row_12h[0], 1)
                earlier_delta = round(prev_p - older_p, 1)
                if earlier_delta > 0.5:
                    prev_direction = "rising"
                elif earlier_delta < -0.5:
                    prev_direction = "falling"
                else:
                    prev_direction = "steady"

                if direction != "steady" and prev_direction == direction:
                    accel = "sustained"
                elif direction != "steady" and prev_direction == "steady":
                    accel = "starting"
                elif direction == "steady" and prev_direction != "steady":
                    accel = "ending"
                elif direction != "steady" and prev_direction != "steady" and prev_direction != direction:
                    accel = "reversing"

        # Consistency: fraction of last 12 readings (1 hour) agreeing on direction
        cursor = await db.execute(
            "SELECT pressure FROM weather_readings ORDER BY id DESC LIMIT 12"
        )
        recent_12 = [r[0] for r in await cursor.fetchall()]
        consistent = 0
        for i in range(1, len(recent_12)):
            diff = recent_12[i-1] - recent_12[i]
            if (direction == "rising" and diff > -0.1) or \
               (direction == "falling" and diff < 0.1) or \
               (direction == "steady" and abs(diff) < 0.3):
                consistent += 1
        consistency = round(consistent / max(len(recent_12) - 1, 1), 2)

        return {
            "current": current_p,
            "previous": prev_p,
            "delta": delta,
            "direction": direction,
            "acceleration": accel,
            "consistency": consistency,
        }
    finally:
        await db.close()


async def get_recent_trend(column: str, hours: int = 3) -> dict | None:
    """Get the change in a column (temperature/humidity) over N hours."""
    if column not in ("temperature", "humidity"):
        return None
    db = await get_db()
    try:
        cursor = await db.execute(
            f"SELECT {column} FROM weather_readings ORDER BY id DESC LIMIT 1"
        )
        latest = await cursor.fetchone()
        if not latest:
            return None

        cutoff = _fmt_cutoff(_now() - timedelta(hours=hours))
        cursor = await db.execute(
            f"SELECT {column} FROM weather_readings WHERE timestamp >= ? ORDER BY id ASC LIMIT 1",
            (cutoff,),
        )
        earlier = await cursor.fetchone()
        current_val = round(latest[0], 1)
        prev_val = round(earlier[0], 1) if earlier else current_val
        delta = round(current_val - prev_val, 1)

        return {"current": current_val, "previous": prev_val, "delta": delta}
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


async def get_daily_extremes() -> dict | None:
    """Return the days with highest/lowest average temperature and humidity.

    Only considers complete days (excludes today) so the current partial day
    doesn't temporarily claim records during the morning.
    """
    db = await get_db()
    try:
        today_str = _now().strftime("%Y-%m-%d")
        cursor = await db.execute("""
            WITH daily AS (
                SELECT date(timestamp) as day,
                       AVG(temperature) as temp_avg,
                       AVG(humidity) as hum_avg
                FROM weather_readings
                WHERE date(timestamp) < ?
                GROUP BY date(timestamp)
            )
            SELECT
                (SELECT day FROM daily ORDER BY temp_avg DESC LIMIT 1) as hottest_avg_day,
                (SELECT temp_avg FROM daily ORDER BY temp_avg DESC LIMIT 1) as hottest_avg,
                (SELECT day FROM daily ORDER BY temp_avg ASC LIMIT 1) as coldest_avg_day,
                (SELECT temp_avg FROM daily ORDER BY temp_avg ASC LIMIT 1) as coldest_avg,
                (SELECT day FROM daily ORDER BY hum_avg DESC LIMIT 1) as most_humid_avg_day,
                (SELECT hum_avg FROM daily ORDER BY hum_avg DESC LIMIT 1) as most_humid_avg,
                (SELECT day FROM daily ORDER BY hum_avg ASC LIMIT 1) as least_humid_avg_day,
                (SELECT hum_avg FROM daily ORDER BY hum_avg ASC LIMIT 1) as least_humid_avg
        """, (today_str,))
        row = await cursor.fetchone()
        return dict(row) if row and row["hottest_avg_day"] else None
    finally:
        await db.close()


async def get_climate_stats() -> dict:
    """Return monthly (all-time + per-year) and yearly temperature averages.

    Only considers complete days so the current partial day doesn't skew averages.
    """
    db = await get_db()
    try:
        today_str = _now().strftime("%Y-%m-%d")

        # All-time monthly aggregates (for the climate chart)
        cursor = await db.execute("""
            SELECT
                CAST(strftime('%m', timestamp) AS INTEGER) as month,
                ROUND(AVG(temperature), 1) as temp_avg,
                ROUND(MIN(temperature), 1) as temp_min,
                ROUND(MAX(temperature), 1) as temp_max,
                ROUND(AVG(humidity), 1) as hum_avg,
                COUNT(*) as readings
            FROM weather_readings
            WHERE date(timestamp) < ?
            GROUP BY month
            ORDER BY month
        """, (today_str,))
        rows = await cursor.fetchall()
        db_monthly = {r["month"]: dict(r) for r in rows}

        monthly_all = []
        for m in range(1, 13):
            if m in db_monthly:
                monthly_all.append(db_monthly[m])
            else:
                monthly_all.append({"month": m, "temp_avg": None, "temp_min": None, "temp_max": None, "hum_avg": None, "readings": 0})

        # Per-year monthly data
        cursor = await db.execute("""
            SELECT
                CAST(strftime('%Y', timestamp) AS INTEGER) as year,
                CAST(strftime('%m', timestamp) AS INTEGER) as month,
                ROUND(AVG(temperature), 1) as temp_avg,
                ROUND(MIN(temperature), 1) as temp_min,
                ROUND(MAX(temperature), 1) as temp_max,
                ROUND(AVG(humidity), 1) as hum_avg,
                COUNT(*) as readings
            FROM weather_readings
            WHERE date(timestamp) < ?
            GROUP BY year, month
            ORDER BY year, month
        """, (today_str,))
        per_year = await cursor.fetchall()

        years_set = set()
        by_year_month = {}  # (year, month) -> row
        for r in per_year:
            y, m = r["year"], r["month"]
            years_set.add(y)
            by_year_month[(y, m)] = dict(r)

        years = sorted(years_set)
        monthly_by_year = {}
        for y in years:
            months = []
            for m in range(1, 13):
                key = (y, m)
                if key in by_year_month:
                    months.append(by_year_month[key])
                else:
                    months.append({"month": m, "temp_avg": None, "temp_min": None, "temp_max": None, "hum_avg": None, "readings": 0})
            monthly_by_year[str(y)] = months

        # Yearly aggregates
        cursor = await db.execute("""
            SELECT
                CAST(strftime('%Y', timestamp) AS INTEGER) as year,
                ROUND(AVG(temperature), 1) as temp_avg,
                ROUND(MIN(temperature), 1) as temp_min,
                ROUND(MAX(temperature), 1) as temp_max,
                ROUND(AVG(humidity), 1) as hum_avg,
                COUNT(*) as readings
            FROM weather_readings
            WHERE date(timestamp) < ?
            GROUP BY year
            ORDER BY year
        """, (today_str,))
        yearly = [dict(r) for r in await cursor.fetchall()]

        return {
            "monthly_all": monthly_all,
            "years": years,
            "monthly_by_year": monthly_by_year,
            "yearly": yearly,
        }
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
