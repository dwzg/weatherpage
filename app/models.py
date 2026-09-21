"""Request and response schemas.

Validation matters more here than in most small apps: the sensor feed is
unattended, and a single bad reading (Home Assistant sending ``unavailable``,
or a sensor glitching to 9999) would otherwise be stored forever and skew
every chart and all-time record.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, Field, field_validator, model_validator

from . import clock

# Plausible ranges for a balcony sensor. Deliberately generous — wide enough
# for any real weather on Earth, tight enough to reject a malfunction.
TEMPERATURE_RANGE = (-90.0, 60.0)
HUMIDITY_RANGE = (0.0, 100.0)
PRESSURE_RANGE = (800.0, 1100.0)


def _coerce_float(value: Any) -> Any:
    """Accept the quoted numbers Home Assistant and the relay workflow send."""
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError("value is empty")
        return stripped
    return value


class ReadingIn(BaseModel):
    """A single incoming sensor reading."""

    temperature: Annotated[float, Field(ge=TEMPERATURE_RANGE[0], le=TEMPERATURE_RANGE[1])]
    humidity: Annotated[float, Field(ge=HUMIDITY_RANGE[0], le=HUMIDITY_RANGE[1])]
    pressure: Annotated[float, Field(ge=PRESSURE_RANGE[0], le=PRESSURE_RANGE[1])]
    timestamp: str
    #: Minutes east of UTC for the instant this reading was taken. Normally
    #: derived below and not sent: it exists because the stored timestamp is a
    #: local wall clock, which names two instants during the autumn DST
    #: fallback. A caller that knows which one it means — a backfill replaying
    #: that hour — can say so, either here or as an offset on ``timestamp``.
    utc_offset: Annotated[int | None, Field(ge=-1440, le=1440)] = None

    @model_validator(mode="before")
    @classmethod
    def _resolve_offset(cls, data: Any) -> Any:
        """Read the offset off the raw timestamp, before it is normalised away.

        ``normalise_ts`` keeps the wall clock and drops any offset suffix, so
        this has to happen on the way in. A timestamp that will not parse is
        left alone for the field validator to reject with a useful message.
        """
        if not isinstance(data, dict) or data.get("utc_offset") is not None:
            return data
        try:
            offset = clock.resolve_offset(str(data.get("timestamp", "")),
                                          arrival=clock.now())
        except (TypeError, ValueError):
            return data
        return {**data, "utc_offset": offset}

    @field_validator("temperature", "humidity", "pressure", mode="before")
    @classmethod
    def _strings_are_numbers(cls, value: Any) -> Any:
        return _coerce_float(value)

    @field_validator("timestamp")
    @classmethod
    def _timestamp_is_parseable(cls, value: str) -> str:
        """Reject anything that is not a timestamp, and normalise the rest."""
        try:
            return clock.normalise_ts(value)
        except ValueError as exc:
            raise ValueError(
                "timestamp must be ISO-8601, e.g. 2026-06-20T21:00:00"
            ) from exc


class ReadingAccepted(BaseModel):
    """Result of storing a reading."""

    status: str = "ok"
    timestamp: str


class CleanupResult(BaseModel):
    """Result of a cleanup call."""

    status: str = "ok"
    deleted: int
