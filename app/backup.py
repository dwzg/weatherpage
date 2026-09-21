"""Nightly snapshots of the reading archive.

The archive is the one thing in this project that cannot be rebuilt. Code can
be recovered from git, the model can be refitted, the image can be rebuilt
from a tag — but a reading that was not taken is gone, and a reading that was
deleted is gone with it. Until now the database lived as a single file on a
single bind mount with no copy anywhere.

This is the cheap half of the answer: a compact copy of the database, taken
once a day into a directory beside it, keeping the last
:data:`KEEP_BACKUPS`. It covers the failures that actually happen to a small
self-hosted app — a mistaken ``DELETE /api/weather/cleanup`` over the wrong
range, a corrupted page, a bad migration — and costs a few seconds and a few
tens of megabytes.

It does *not* cover losing the volume, because the copies are on it. That is
what ``.github/workflows/backup.yml`` is for: it pages ``/api/weather/export``
from outside and keeps the result off this machine entirely. Two layers,
because they fail in different ways.

``VACUUM INTO`` rather than a file copy: it is SQLite's own supported way to
snapshot a live database, it is safe against a concurrent writer, it resolves
the WAL rather than leaving a copy that needs one, and what it writes is
defragmented — so the copy is smaller than the original and is a valid
database the moment the statement returns.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

from . import clock, database
from .config import get_settings

log = logging.getLogger(__name__)

#: Where snapshots go, beside the live database inside the data volume.
BACKUP_DIR_NAME = "backups"

#: Snapshots are named for the day they were taken.
BACKUP_PREFIX = "weather-"
BACKUP_SUFFIX = ".db"

#: How many daily snapshots to keep. A week is long enough that a mistake
#: made on a Friday is still recoverable on the following Thursday, and short
#: enough that the copies stay a rounding error against the volume.
KEEP_BACKUPS = 7

#: Local time of day to take one. Deliberately not midnight: the rollup and
#: several cached aggregates turn over then, and there is nothing to be
#: gained from doing both at once.
BACKUP_HOUR = 3
BACKUP_MINUTE = 30


def backup_dir() -> Path:
    """Where snapshots live. Inside the data volume, so they persist."""
    return get_settings().data_dir / BACKUP_DIR_NAME


def backup_path(day: str) -> Path:
    return backup_dir() / f"{BACKUP_PREFIX}{day}{BACKUP_SUFFIX}"


def existing_backups() -> list[Path]:
    """Every snapshot, oldest first. The name sorts chronologically."""
    directory = backup_dir()
    if not directory.is_dir():
        return []
    return sorted(directory.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}"))


def prune(keep: int = KEEP_BACKUPS) -> list[Path]:
    """Delete all but the newest ``keep`` snapshots. Returns what went."""
    backups = existing_backups()
    removed = []
    for stale in backups[: max(len(backups) - keep, 0)]:
        try:
            stale.unlink()
            removed.append(stale)
        except OSError:
            log.warning("backup: could not remove %s", stale, exc_info=True)
    return removed


async def take_snapshot(day: str | None = None) -> Path | None:
    """Write one snapshot, or ``None`` if today's already exists.

    ``VACUUM INTO`` refuses to overwrite, which is the behaviour we want:
    a restart should not spend seconds rewriting a copy it already has.
    """
    day = day or clock.fmt_date(clock.now())
    target = backup_path(day)
    if target.exists():
        return None

    target.parent.mkdir(parents=True, exist_ok=True)
    async with database.acquire() as db:
        # A bound parameter is not allowed here, and the path is built from
        # DATA_DIR and a formatted date rather than from anything a request
        # can reach. Quotes are doubled anyway, so a data directory with an
        # apostrophe in it cannot end the string literal early.
        escaped = str(target).replace("'", "''")
        await db.execute(f"VACUUM INTO '{escaped}'")
    return target


async def run_once() -> Path | None:
    """Take today's snapshot and prune old ones. Never raises."""
    try:
        written = await take_snapshot()
    except Exception:
        log.exception("backup: snapshot failed")
        return None

    if written is not None:
        log.info("backup: wrote %s (%.1f MB)",
                 written, written.stat().st_size / 1_048_576)
    for removed in prune():
        log.info("backup: removed old snapshot %s", removed)
    return written


def seconds_until_next_run(reference=None) -> float:
    """How long until the next :data:`BACKUP_HOUR`:``BACKUP_MINUTE`` locally.

    Computed from the configured timezone rather than from a fixed 24-hour
    interval, so the hour does not drift across a DST change — the same
    reason nothing else here uses a bare ``datetime.now()``.
    """
    now = reference if reference is not None else clock.now()
    target = now.replace(hour=BACKUP_HOUR, minute=BACKUP_MINUTE,
                         second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def scheduler() -> None:
    """Take a snapshot once a day, for as long as the app is running.

    Kept in-process rather than asking for a cron in the container: this app
    is one image with one process by design, and a backup that depends on
    another moving part is a backup that stops without telling anyone.

    A failure is logged and the loop carries on — a broken snapshot must
    never be the reason the dashboard stops serving.
    """
    log.info("backup: snapshots at %02d:%02d local, keeping %d",
             BACKUP_HOUR, BACKUP_MINUTE, KEEP_BACKUPS)
    while True:
        try:
            await asyncio.sleep(seconds_until_next_run())
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Belt and braces: run_once swallows its own failures, so this
            # only catches something going wrong in the timing itself.
            log.exception("backup: scheduler error; continuing")
            await asyncio.sleep(60)
