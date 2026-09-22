"""JSON API routes."""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from . import clock, database, services
from .config import Settings, get_settings
from .models import CleanupResult, ReadingAccepted, ReadingIn

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/weather", tags=["weather"])

PeriodQuery = Query("24h", pattern=clock.PERIOD_PATTERN)


async def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    settings: Settings = Depends(get_settings),
) -> None:
    """Guard the write endpoints.

    When ``API_KEY`` is unset the endpoints are open, which is how local
    development works. The comparison is constant-time so a wrong key cannot
    be recovered by timing the response.
    """
    if not settings.requires_api_key:
        return
    if x_api_key is None or not secrets.compare_digest(x_api_key, settings.api_key or ""):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key"
        )


@router.post("", response_model=ReadingAccepted, dependencies=[Depends(require_api_key)])
@router.post("/", response_model=ReadingAccepted, dependencies=[Depends(require_api_key)],
             include_in_schema=False)
async def post_reading(reading: ReadingIn) -> ReadingAccepted:
    """Store one sensor reading, replacing any reading with the same timestamp."""
    await database.insert_reading(
        temperature=reading.temperature,
        humidity=reading.humidity,
        pressure=reading.pressure,
        timestamp=reading.timestamp,
        utc_offset=reading.utc_offset,
    )

    # Record what the page now says, for later scoring against what the
    # weather actually did. Never at the expense of the reading: the reading
    # is the irreplaceable thing and is already committed above, so a failure
    # to log is a warning, not a 500 handed back to the relay — which would
    # make Home Assistant retry a reading that was in fact stored.
    try:
        if reading.utc_offset is not None:
            await services.record_prediction(reading.timestamp, reading.utc_offset)
    except Exception:
        log.warning("could not log the prediction for %s", reading.timestamp,
                    exc_info=True)

    return ReadingAccepted(timestamp=reading.timestamp)


@router.delete("/cleanup", response_model=CleanupResult,
               dependencies=[Depends(require_api_key)])
async def cleanup(
    from_ts: str | None = Query(None, alias="from"),
    to_ts: str | None = Query(None, alias="to"),
) -> CleanupResult:
    """Prune readings.

    With ``from`` and ``to``, deletes every reading in that range. Without
    them, deletes readings that are off the expected minute grid.
    """
    if bool(from_ts) != bool(to_ts):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'from' and 'to' must be given together",
        )
    if from_ts and to_ts:
        try:
            deleted = await database.remove_readings_in_range(from_ts, to_ts)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
    else:
        deleted = await database.remove_off_grid_readings()
    return CleanupResult(deleted=deleted)


@router.get("/current")
async def get_current() -> dict:
    """The most recent reading."""
    current = await database.get_current()
    if not current:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no readings yet"
        )
    return current


@router.get("/status")
async def get_status() -> dict:
    """Everything the dashboard polls: current values, trends and forecast."""
    payload = await services.build_status()
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no readings yet"
        )
    return payload


@router.get("/export", dependencies=[Depends(require_api_key)])
async def export(
    after: str | None = Query(None, description="exclusive cursor timestamp"),
    after_offset: int | None = Query(None, description="that cursor's utc_offset"),
    to_ts: str | None = Query(None, alias="to", description="inclusive upper bound"),
    limit: int = Query(
        database.EXPORT_PAGE_SIZE, ge=1, le=database.EXPORT_MAX_PAGE_SIZE
    ),
) -> dict:
    """Raw readings for the retraining job, paged oldest first.

    Deliberately separate from ``/history``, which downsamples above
    TARGET_CHART_POINTS: this one never does, which is the whole point of it.
    It is behind the API key because an unbucketed archive is a far larger
    response than anything else here, not because the numbers are secret.

    ``next`` carries the cursor for the following page, or is ``null`` when
    the last page came back short. The cursor is a ``(timestamp, offset)``
    pair because a timestamp alone is not unique across the repeated hour of
    the autumn DST fallback.
    """
    if (after is None) != (after_offset is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'after' and 'after_offset' must be given together",
        )
    try:
        readings = await database.export_readings(
            after=(after, after_offset) if after is not None else None,
            until=to_ts,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    # A short page is the last one. A full page might be exactly the end of
    # the data, in which case the caller spends one more request finding out.
    last = readings[-1] if len(readings) == limit else None
    return {
        "readings": readings,
        "next": (
            {"after": last["timestamp"], "after_offset": last["utc_offset"]}
            if last
            else None
        ),
    }


@router.get("/predictions", dependencies=[Depends(require_api_key)])
async def predictions(
    after: str | None = Query(None, description="exclusive cursor timestamp"),
    after_offset: int | None = Query(None, description="that cursor's utc_offset"),
    limit: int = Query(
        database.EXPORT_PAGE_SIZE, ge=1, le=database.EXPORT_MAX_PAGE_SIZE
    ),
) -> dict:
    """What the page said, hour by hour, for the retraining job to score.

    Paged exactly like ``/export`` and behind the same key, for the same
    reasons: the cursor is a ``(timestamp, utc_offset)`` pair because a
    timestamp is not unique across the repeated autumn hour, and an
    unbounded archive is a large response.

    Carries no observations. Those belong to Open-Meteo, and the trainer
    already fetches them for its labels — storing a second copy here would
    be a cache of somebody else's data that could silently go stale.
    """
    if (after is None) != (after_offset is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'after' and 'after_offset' must be given together",
        )
    try:
        logged = await database.export_predictions(
            after=(after, after_offset) if after is not None else None,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    last = logged[-1] if len(logged) == limit else None
    return {
        "predictions": logged,
        "next": (
            {"after": last["timestamp"], "after_offset": last["utc_offset"]}
            if last
            else None
        ),
    }


@router.get("/history")
async def get_history(period: str = PeriodQuery) -> dict:
    """Readings for ``period``.

    Long periods are averaged into fixed-width buckets so the response stays
    small; ``interval_seconds`` reports the spacing and ``bucketed`` says
    whether the points are averages. Bucketed points carry ``*_min`` and
    ``*_max`` alongside the average.
    """
    series = await database.get_history_series(period)
    return series.as_dict()


@router.get("/stats")
async def get_stats(period: str = PeriodQuery) -> dict:
    """Min/max/avg for each metric over ``period``."""
    return await database.get_stats(period)


@router.get("/daily")
async def get_daily(months: int = Query(3, ge=1, le=24)) -> list[dict]:
    """Per-day summaries for the calendar heatmap."""
    return await database.get_daily_summaries(months)
