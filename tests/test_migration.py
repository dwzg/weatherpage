"""The startup migration must repair a pre-existing database, not destroy it."""

import sqlite3

import pytest

LEGACY_SCHEMA = """
CREATE TABLE weather_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    temperature REAL NOT NULL,
    humidity REAL NOT NULL,
    pressure REAL NOT NULL,
    timestamp TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_timestamp ON weather_readings(timestamp);
"""


@pytest.fixture
def legacy_db(tmp_path):
    """A database in the old shape, containing duplicate timestamps."""
    path = tmp_path / "weather.db"
    con = sqlite3.connect(path)
    con.executescript(LEGACY_SCHEMA)
    con.executemany(
        "INSERT INTO weather_readings (temperature, humidity, pressure, timestamp) "
        "VALUES (?, ?, ?, ?)",
        [
            (20.0, 50.0, 1013.0, "2026-06-20 12:00:00"),  # kept (earliest id)
            (99.0, 50.0, 1013.0, "2026-06-20 12:00:00"),  # duplicate
            (98.0, 50.0, 1013.0, "2026-06-20 12:00:00"),  # duplicate
            (21.0, 51.0, 1014.0, "2026-06-20 12:05:00"),
            (22.0, 52.0, 1015.0, "2026-06-20 12:10:00"),
        ],
    )
    con.commit()
    con.close()
    return path


class TestMigration:
    async def test_dedupes_and_keeps_the_earliest_row(self, legacy_db, db):
        history = await db.get_history("all")
        assert len(history) == 3, "duplicates should be collapsed"
        assert history[0]["temperature"] == 20.0, "the earliest row wins, as cleanup always did"

    async def test_real_readings_survive(self, legacy_db, db):
        timestamps = [r["timestamp"] for r in await db.get_history("all")]
        assert timestamps == [
            "2026-06-20 12:00:00", "2026-06-20 12:05:00", "2026-06-20 12:10:00",
        ]

    async def test_timestamp_is_unique_afterwards(self, legacy_db, db):
        async with db.acquire() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='index' AND name='idx_timestamp_unique'")
            assert (await cursor.fetchone())[0] == 1

    async def test_ingestion_upserts_after_migration(self, legacy_db, db):
        await db.insert_reading(30.0, 60.0, 1020.0, "2026-06-20T12:00:00")
        history = await db.get_history("all")
        assert len(history) == 3
        assert history[0]["temperature"] == 30.0

    async def test_migration_is_idempotent(self, legacy_db, db):
        async with db.acquire() as conn:
            await db._migrate(conn)
            await db._migrate(conn)
        assert len(await db.get_history("all")) == 3


class TestFreshDatabase:
    async def test_starts_empty_and_usable(self, db):
        assert await db.get_history("all") == []
        await db.insert_reading(20.0, 50.0, 1013.0, "2026-06-20T12:00:00")
        assert len(await db.get_history("all")) == 1
