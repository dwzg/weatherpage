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


LEGACY_UNIQUE_SCHEMA = LEGACY_SCHEMA + """
CREATE UNIQUE INDEX idx_timestamp_unique ON weather_readings(timestamp);
"""


@pytest.fixture
def pre_offset_db(tmp_path):
    """A database from after timestamps became unique but before the offset.

    Its unique index carries the name the current one uses, over ``timestamp``
    alone — so a ``CREATE UNIQUE INDEX IF NOT EXISTS`` would quietly leave the
    repeated autumn hour collapsing into a single row.
    """
    path = tmp_path / "weather.db"
    con = sqlite3.connect(path)
    con.executescript(LEGACY_UNIQUE_SCHEMA)
    con.executemany(
        "INSERT INTO weather_readings (temperature, humidity, pressure, timestamp) "
        "VALUES (?, ?, ?, ?)",
        [
            (20.0, 50.0, 1013.0, "2026-06-20 12:00:00"),   # summer: +02:00
            (2.0, 80.0, 1005.0, "2026-01-10 08:00:00"),    # winter: +01:00
            (9.0, 90.0, 1010.0, "2026-10-25 02:30:00"),    # the repeated hour
        ],
    )
    con.commit()
    con.close()
    return path


class TestOffsetMigration:
    async def test_existing_rows_get_the_offset_of_their_own_season(self, pre_offset_db, db):
        async with db.acquire() as conn:
            cursor = await conn.execute(
                f"SELECT timestamp, utc_offset FROM weather_readings {db.ORDER_OLDEST_FIRST}"
            )
            rows = [(r["timestamp"], r["utc_offset"]) for r in await cursor.fetchall()]
        assert rows == [
            ("2026-01-10 08:00:00", 60),
            ("2026-06-20 12:00:00", 120),
            # Nothing recorded which pass this was, so it is read as the first.
            ("2026-10-25 02:30:00", 120),
        ]

    async def test_readings_are_not_lost(self, pre_offset_db, db):
        assert len(await db.get_history("all")) == 3

    async def test_the_unique_index_is_rebuilt_over_both_columns(self, pre_offset_db, db):
        async with db.acquire() as conn:
            definition = await db._index_definition(conn, "idx_timestamp_unique")
        assert definition is not None and "utc_offset" in definition

    async def test_the_repeated_hour_can_then_be_completed(self, pre_offset_db, db):
        await db.insert_reading(8.0, 92.0, 1011.0, "2026-10-25 02:30:00", utc_offset=60)
        assert len(await db.get_history("all")) == 4

    async def test_migration_is_idempotent(self, pre_offset_db, db):
        async with db.acquire() as conn:
            await db._migrate(conn)
            await db._migrate(conn)
        assert len(await db.get_history("all")) == 3


class TestFreshDatabase:
    async def test_starts_empty_and_usable(self, db):
        assert await db.get_history("all") == []
        await db.insert_reading(20.0, 50.0, 1013.0, "2026-06-20T12:00:00")
        assert len(await db.get_history("all")) == 1


async def _plan(conn, sql: str) -> str:
    """The query plan for ``sql``, as one string to search."""
    cursor = await conn.execute(f"EXPLAIN QUERY PLAN {sql}")
    return " | ".join(row["detail"] for row in await cursor.fetchall())


class TestOrderingUsesAnIndex:
    """Both ordering constants must be answered from the index, not a sort.

    They mix directions, so an index on ``(timestamp, utc_offset)`` cannot
    serve them: SQLite falls back to scanning the table and sorting it, which
    is invisible until the archive is large and then costs ~35 ms on every
    "latest reading" query — on the path every page render and every poll
    takes. Nothing else in the suite would notice that coming back, because
    the answers stay correct; only the plan changes.
    """

    async def test_the_index_is_created_with_both_directions(self, db):
        async with db.acquire() as conn:
            definition = await db._index_definition(conn, "idx_timestamp_desc")
        assert definition is not None
        assert "timestamp DESC" in definition and "utc_offset ASC" in definition

    @pytest.mark.parametrize("order", ["ORDER_NEWEST_FIRST", "ORDER_OLDEST_FIRST"])
    async def test_neither_ordering_falls_back_to_a_sort(self, db, order):
        sql = (
            "SELECT timestamp, temperature, humidity, pressure "
            f"FROM weather_readings {getattr(db, order)} LIMIT 1"
        )
        async with db.acquire() as conn:
            plan = await _plan(conn, sql)
        assert "idx_timestamp_desc" in plan, plan
        assert "TEMP B-TREE" not in plan, plan

    async def test_range_scans_still_use_an_index(self, db):
        """The dropped idx_timestamp served these; this one has to as well."""
        sql = (
            "SELECT timestamp, temperature FROM weather_readings "
            f"WHERE timestamp >= '2026-06-20 00:00:00' {db.ORDER_OLDEST_FIRST}"
        )
        async with db.acquire() as conn:
            plan = await _plan(conn, sql)
        assert "idx_timestamp_desc" in plan, plan

    async def test_the_superseded_index_is_gone(self, legacy_db, db):
        async with db.acquire() as conn:
            assert await db._index_definition(conn, "idx_timestamp") is None
