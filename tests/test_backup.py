"""Snapshots of the archive — the one thing here that cannot be rebuilt."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from app import backup, clock


async def seed(db, count: int = 5) -> None:
    for minute in range(count):
        await db.insert_reading(
            20.0 + minute, 55.0, 1013.0,
            f"2026-06-20 12:{minute * 5:02d}:00",
        )


class TestSnapshot:
    async def test_it_writes_a_usable_database(self, db):
        await seed(db)
        written = await backup.take_snapshot("2026-06-20")

        assert written is not None and written.exists()
        con = sqlite3.connect(written)
        try:
            rows = con.execute("SELECT COUNT(*) FROM weather_readings").fetchone()[0]
            # Not just readable — the copy carries the schema too, or a
            # restore would come back without the index or the rollup.
            indexes = {
                r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'")
            }
        finally:
            con.close()

        assert rows == 5
        assert "idx_timestamp_desc" in indexes
        assert "idx_timestamp_unique" in indexes

    async def test_the_rollup_comes_with_it(self, db):
        await seed(db)
        written = await backup.take_snapshot("2026-06-20")
        con = sqlite3.connect(written)
        try:
            days = con.execute("SELECT COUNT(*) FROM daily_rollup").fetchone()[0]
        finally:
            con.close()
        assert days == 1

    async def test_the_prediction_log_comes_with_it(self, db):
        """It is the one table that cannot be rebuilt from the readings —
        a replay would attribute every hour to today's model — so a backup
        that dropped it would quietly lose the only record of what the page
        actually said."""
        await seed(db)
        await db.insert_prediction(
            "2026-06-20 12:00:00", 120, 0.42, None, "Rain possible", "2026-06-01"
        )
        written = await backup.take_snapshot("2026-06-20")
        con = sqlite3.connect(written)
        try:
            rows = con.execute(
                "SELECT timestamp, rain_probability, forecast FROM prediction_log"
            ).fetchall()
        finally:
            con.close()
        assert rows == [("2026-06-20 12:00:00", 0.42, "Rain possible")]

    async def test_it_does_not_overwrite_todays(self, db):
        """VACUUM INTO refuses an existing file, which is the behaviour we
        want: a restart should not rewrite a copy it already has."""
        await seed(db)
        first = await backup.take_snapshot("2026-06-20")
        assert first is not None
        assert await backup.take_snapshot("2026-06-20") is None

    async def test_a_failure_is_swallowed(self, db, monkeypatch):
        """A broken snapshot must never be why the dashboard stops."""
        async def boom(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(backup, "take_snapshot", boom)
        assert await backup.run_once() is None


class TestRetention:
    def _make(self, day: str) -> None:
        backup.backup_dir().mkdir(parents=True, exist_ok=True)
        backup.backup_path(day).write_bytes(b"not really a database")

    def test_it_keeps_the_newest(self, _isolated_settings):
        days = [f"2026-06-{day:02d}" for day in range(1, 11)]
        for day in days:
            self._make(day)

        backup.prune(keep=7)

        kept = [p.name for p in backup.existing_backups()]
        assert len(kept) == 7
        assert kept[0] == "weather-2026-06-04.db"
        assert kept[-1] == "weather-2026-06-10.db"

    def test_nothing_to_prune_is_fine(self, _isolated_settings):
        self._make("2026-06-01")
        assert backup.prune(keep=7) == []
        assert len(backup.existing_backups()) == 1

    def test_a_missing_directory_is_not_an_error(self, _isolated_settings):
        assert backup.existing_backups() == []
        assert backup.prune() == []


class TestSchedule:
    """The hour is computed from the configured timezone, not from a fixed
    24-hour interval, so it cannot drift across a DST change."""

    @pytest.mark.parametrize("now,expected_hours", [
        (datetime(2026, 6, 20, 0, 30), 3.0),    # before it, same day
        (datetime(2026, 6, 20, 3, 29), 0.0166),  # a minute before
        (datetime(2026, 6, 20, 3, 30), 24.0),   # exactly on it: tomorrow
        (datetime(2026, 6, 20, 12, 30), 15.0),  # after it, so tomorrow
    ])
    def test_it_waits_for_the_next_one(self, now, expected_hours):
        seconds = backup.seconds_until_next_run(now)
        assert seconds == pytest.approx(expected_hours * 3600, abs=60)

    def test_it_never_returns_a_time_in_the_past(self):
        for hour in range(24):
            moment = datetime(2026, 6, 20, hour, 15)
            assert 0 < backup.seconds_until_next_run(moment) <= 86_400


class TestItRunsWithTheApp:
    async def test_the_scheduler_starts_and_stops_with_the_lifespan(self, client):
        """It borrows a pooled connection, so it must start after the pool
        opens and be cancelled before it closes."""
        import asyncio

        running = [
            task for task in asyncio.all_tasks()
            if task.get_coro().__qualname__.startswith("scheduler")
        ]
        assert running, "the backup scheduler should be running"

    async def test_a_snapshot_taken_now_lands_in_the_data_volume(self, db):
        """Beside the database, so it survives a container restart — and,
        being on the same volume, is explicitly not the off-host copy."""
        await seed(db)
        written = await backup.take_snapshot()
        assert written is not None
        assert written.parent == backup.backup_dir()
        assert written.name == f"weather-{clock.fmt_date(clock.now())}.db"
        assert written.parent.parent == db.get_settings().data_dir


class TestTheOffHostBackupUsesTheRawArchive:
    """The nightly workflow must read /export, not /history.

    Same trap ml/train.py fell into: /history downsamples above
    TARGET_CHART_POINTS, so a "backup" taken from it would be a few hundred
    bucket averages wearing the name of an archive — and would look fine
    until someone needed it.
    """

    def test_it_pages_the_export_endpoint(self):
        from pathlib import Path

        workflow = (Path(__file__).resolve().parent.parent
                    / ".github" / "workflows" / "backup.yml").read_text()
        assert "/api/weather/export" in workflow
        assert "/api/weather/history" not in workflow
        # A single page would silently keep only the oldest 50k readings.
        assert "after_offset" in workflow


@pytest.fixture
def _patched_day(monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 6, 20, 4, 0))
    yield


def test_backup_names_sort_chronologically():
    """Retention relies on it: the newest N by name must be the newest N."""
    days = [(datetime(2026, 1, 1) + timedelta(days=n)).strftime("%Y-%m-%d")
            for n in range(0, 400, 37)]
    names = [f"{backup.BACKUP_PREFIX}{day}{backup.BACKUP_SUFFIX}" for day in days]
    assert names == sorted(names)
