"""The nowcast's feature vector, as a pure function of a run of readings.

One implementation with two callers, which is the whole point of the module:

* the app (:func:`app.services.nowcast_features`) hands it the last
  :data:`HISTORY_DAYS` of this balcony's readings out of SQLite;
* the trainer (``ml/train.py``) hands it years of ten-minute observations from
  the German weather service's own stations, one station at a time, and asks
  for the vector at every hour.

So a feature cannot come to mean one thing in training and another in the
browser: there is no second copy of the arithmetic to drift. Nothing here
touches the database, the clock or the network — ``now`` is an argument, and
so is the daily pressure cycle the readings are corrected by.

Every window is defined by time, never by a count of readings. The balcony
reports every five minutes and the weather service every ten; a window of
"the last six readings" would be half an hour on one and an hour on the other.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .weather import compute_dew_point

#: Each end of every comparison is the median over this much time, so one
#: noisy sample cannot push a delta across a split on its own.
SMOOTHING = timedelta(minutes=30)

#: How much history the current pressure is ranked against, the shorter
#: second window, and the hourly readings a ranking needs before it means
#: anything. Shared with :mod:`app.database`, whose percentile on the pressure
#: card is the same ranking.
PERCENTILE_DAYS = 30
SHORT_PERCENTILE_DAYS = 7
PERCENTILE_MIN_READINGS = 7 * 24
#: Fraction of a shorter window that must be populated for it to count.
PERCENTILE_COVERAGE = 0.6

#: How far back :func:`compute` reads. A caller that hands it less gets a
#: shorter ranking window and, until a week is there, no vector at all.
HISTORY_DAYS = PERCENTILE_DAYS

#: Humidity at or above which the sensor counts as saturated. A wet sensor in
#: rain reads 97-100 %, and how long it has sat there is the nearest thing to
#: a rain gauge this station has.
SATURATED_HUMIDITY = 97.0

#: How much of a window its readings must span before its swing means
#: anything: a six-hour range taken over the two hours either side of an
#: outage is a two-hour range, and a clear sky is read off how big it is.
SWING_COVERAGE = 2 / 3

#: The daily pressure cycle: learned over this many whole days before today,
#: from days with at least :data:`CYCLE_MIN_SLOTS` of their 48 half-hour slots
#: populated, and applied only once :data:`CYCLE_MIN_DAYS` such days exist.
#: Slots rather than a reading count, so a ten-minute station and a
#: five-minute one judge a day complete by the same rule.
CYCLE_LEARN_DAYS = 90
CYCLE_MIN_SLOTS = 40
CYCLE_MIN_DAYS = 14
SLOTS_PER_DAY = 48

#: Decimals each feature is rounded to. Part of the model's contract, not
#: cosmetics: the trees split *between* values on this grid, so the trainer
#: can store every threshold to one more decimal and the app, which rounds
#: the same way, lands on the same side of every split as training did.
DECIMALS: dict[str, int] = {
    "rh": 1,
    "rh_max1": 1,
    "rh_max3": 1,
    "sat3": 3,
    "temp": 2,
    "td": 2,
    "spread": 2,
    "dT1": 2,
    "dT3": 2,
    "dT24": 2,
    "dtd3": 2,
    "drh3": 1,
    "pct30": 3,
    "pct7": 3,
    "dp1": 2,
    "dp3": 2,
    "dp6": 2,
    "dp12": 2,
    "t_range3": 1,
    "t_range6": 1,
    "t_range12": 1,
    "t_range24": 1,
    "rh_range6": 1,
    "rh_min24": 1,
    "hour": 0,
}

#: Everything :func:`compute` returns. The model reads whichever of these it
#: was trained on (``model.json`` names them); ``temp``, ``spread`` and
#: ``drh3`` are here for the threshold ladder, which the trainer scores as a
#: baseline on the same hours.
NAMES: tuple[str, ...] = tuple(DECIMALS)


# ── Reading series ─────────────────────────────────────────────────────────


@dataclass
class Series:
    """Readings as parallel lists, oldest first, with the hourly ones indexed.

    ``times`` are naive local wall-clock times, the app's own convention, so
    the hour of the day and the pressure cycle's slots mean the same thing
    for both callers. They need only be non-decreasing: the repeated autumn
    hour puts two readings on one wall-clock time, and the windows below are
    medians and maxima, which do not care about the order inside a tie.
    """

    times: list[datetime]
    temperature: list[float]
    humidity: list[float]
    pressure: list[float]
    #: Indices of the readings on the hour, and their times: the pressure
    #: ranking compares hourly readings, as the card's percentile always has.
    hourly: list[int] = field(default_factory=list)
    hourly_times: list[datetime] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.hourly:
            self.hourly = [i for i, t in enumerate(self.times) if t.minute == 0 and t.second == 0]
            self.hourly_times = [self.times[i] for i in self.hourly]

    @classmethod
    def from_rows(cls, rows: Iterable[tuple[datetime, float, float, float]]) -> Series:
        """Build a series from ``(time, temperature, humidity, pressure)`` rows."""
        times, temperature, humidity, pressure = [], [], [], []
        for moment, t, h, p in rows:
            times.append(moment)
            temperature.append(t)
            humidity.append(h)
            pressure.append(p)
        return cls(times, temperature, humidity, pressure)

    def span(self, start: datetime, end: datetime, *, closed_start: bool = False) -> range:
        """Indices of the readings in ``(start, end]`` (or ``[start, end]``)."""
        lo = (bisect_left if closed_start else bisect_right)(self.times, start)
        return range(lo, bisect_right(self.times, end))


# ── The daily pressure cycle ───────────────────────────────────────────────
#
# A BME280 reduces its reading to sea level using its own temperature, so a
# sensor that bakes in the afternoon sun writes its own day/night rhythm into
# the pressure — around 3 hPa on this balcony, several times the real tide.
# The mean offset of each half-hour slot is learned from whole days and
# subtracted before any pressure feature is taken. On a weather-service
# station, whose barometer is indoors and unreduced, the same arithmetic
# removes the real atmospheric tide instead, which is equally not weather.


def slot_of(moment: datetime) -> int:
    """The half-hour slot of the day a time falls in, 0-47."""
    return moment.hour * 2 + moment.minute // 30


@dataclass
class DaySums:
    """One day's pressures, summed per half-hour slot."""

    sums: list[float] = field(default_factory=lambda: [0.0] * SLOTS_PER_DAY)
    counts: list[int] = field(default_factory=lambda: [0] * SLOTS_PER_DAY)

    def add(self, slot: int, pressure: float) -> None:
        self.sums[slot] += pressure
        self.counts[slot] += 1

    @property
    def complete(self) -> bool:
        return sum(1 for c in self.counts if c) >= CYCLE_MIN_SLOTS


def summarise_days(rows: Iterable[tuple[date, int, float]]) -> dict[date, DaySums]:
    """Per-day slot sums from ``(day, slot, pressure)`` rows.

    Split out from :func:`cycle_from_days` so the trainer can summarise years
    of readings once and slide a 90-day window over the summaries, rather than
    re-reading every reading for every day — same arithmetic, same answer.
    """
    days: dict[date, DaySums] = {}
    for day, slot, pressure in rows:
        days.setdefault(day, DaySums()).add(slot, pressure)
    return days


def cycle_from_days(days: Iterable[DaySums]) -> dict[int, float]:
    """The mean offset of each slot from its day's mean, over complete days.

    Every reading weighs the same, as an ``AVG()`` over the readings would:
    a slot's offset is the sum of its readings' deviations from their day's
    mean over the number of those readings. Returns an empty mapping — no
    correction at all — until :data:`CYCLE_MIN_DAYS` complete days exist,
    because half a cycle is worse than none.
    """
    deviation = [0.0] * SLOTS_PER_DAY
    readings = [0] * SLOTS_PER_DAY
    complete = 0
    for day in days:
        if not day.complete:
            continue
        complete += 1
        mean = sum(day.sums) / sum(day.counts)
        for slot in range(SLOTS_PER_DAY):
            if day.counts[slot]:
                deviation[slot] += day.sums[slot] - day.counts[slot] * mean
                readings[slot] += day.counts[slot]
    if complete < CYCLE_MIN_DAYS:
        return {}
    return {
        slot: round(deviation[slot] / readings[slot], 2)
        for slot in range(SLOTS_PER_DAY)
        if readings[slot]
    }


def pressure_cycle(rows: Iterable[tuple[date, int, float]]) -> dict[int, float]:
    """The daily cycle from ``(day, slot, pressure)`` rows of whole days."""
    return cycle_from_days(summarise_days(rows).values())


def detide(moment: datetime, pressure: float, cycle: Mapping[int, float]) -> float:
    """A pressure with the station's daily cycle removed."""
    return pressure - cycle.get(slot_of(moment), 0.0)


# ── The vector ─────────────────────────────────────────────────────────────


def median(values: Sequence[float]) -> float:
    """The upper median, as the pressure card has always taken it."""
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def rank(value: float, window: Sequence[float]) -> float:
    """Where ``value`` sits in a sorted window, 0-1: the share strictly below it."""
    return round(bisect_left(window, value) / len(window), 3)


def percentile_minimum(days: int) -> int:
    """Hourly readings needed before a ranking window means anything.

    The smaller of a week's worth and most of the window: a rank against a
    handful of readings is meaningless, but demanding every hour would let a
    single afternoon's outage switch the ranking — and the nowcast that reads
    it — off for days.
    """
    return min(PERCENTILE_MIN_READINGS, int(days * 24 * PERCENTILE_COVERAGE))


class _Window:
    """The smoothed values of one series at ``now`` and at lags behind it."""

    def __init__(self, series: Series, now: datetime, cycle: Mapping[int, float]):
        self.series, self.now, self.cycle = series, now, cycle

    def at(self, hours: float) -> tuple[float, float, float] | None:
        """Median temperature, humidity and de-tided pressure ``hours`` ago.

        Over the :data:`SMOOTHING` that ends there, so "now" is the last half
        hour and every lag is the half hour ending that long before it.
        """
        end = self.now - timedelta(hours=hours)
        idx = self.series.span(end - SMOOTHING, end)
        if not idx:
            return None
        s = self.series
        return (
            median([s.temperature[i] for i in idx]),
            median([s.humidity[i] for i in idx]),
            median([detide(s.times[i], s.pressure[i], self.cycle) for i in idx]),
        )

    def covering(self, hours: float, values: list[float]) -> list[float] | None:
        """``values`` over the last ``hours``, if the readings span most of it."""
        idx = self.series.span(self.now - timedelta(hours=hours), self.now)
        if not idx:
            return None
        spanned = self.series.times[idx[-1]] - self.series.times[idx[0]]
        if spanned < timedelta(hours=hours) * SWING_COVERAGE:
            return None
        return [values[i] for i in idx]

    def swing(self, hours: float, values: list[float]) -> float | None:
        """How far ``values`` ranged over the last ``hours``, top to bottom.

        From the raw readings rather than half-hour medians: the range is the
        signal here, and a clear sky shows in how far the temperature climbs
        by day and falls by night — up to twice an overcast sky's swing.
        """
        window = self.covering(hours, values)
        return None if window is None else max(window) - min(window)

    def humidities(self, hours: float) -> list[float]:
        idx = self.series.span(self.now - timedelta(hours=hours), self.now)
        return [self.series.humidity[i] for i in idx]

    def ranking(self, days: int) -> list[float] | None:
        """Sorted de-tided hourly pressures over the last ``days``, if enough."""
        s = self.series
        lo = bisect_left(s.hourly_times, self.now - timedelta(days=days))
        hi = bisect_right(s.hourly_times, self.now)
        if hi - lo < percentile_minimum(days):
            return None
        return sorted(
            detide(s.times[i], s.pressure[i], self.cycle) for i in s.hourly[lo:hi]
        )


def compute(
    series: Series, now: datetime, cycle: Mapping[int, float]
) -> dict[str, float | None] | None:
    """The full vector at ``now``, or ``None`` if there is nothing to say yet.

    ``None`` means no reading in the last half hour, or under a week of
    history to rank the pressure against — the honest answer on a young
    database. Past that, a feature whose lag falls in an outage comes back
    ``None`` on its own rather than sinking the whole vector: the model routes
    a missing value down the branch training sent it, and a short gap 24 hours
    ago is no reason to stop forecasting now.
    """
    w = _Window(series, now, cycle)
    current = w.at(0)
    long_rank = w.ranking(PERCENTILE_DAYS)
    if current is None or long_rank is None:
        return None
    short_rank = w.ranking(SHORT_PERCENTILE_DAYS)
    t, h, p = current
    dew = compute_dew_point(t, h)

    ago = {lag: w.at(lag) for lag in (1, 3, 6, 12, 24)}
    recent3 = w.humidities(3)
    recent1 = w.humidities(1)

    def change(lag: int, index: int) -> float | None:
        then = ago[lag]
        return None if then is None else current[index] - then[index]

    then3 = ago[3]
    values: dict[str, float | None] = {
        "rh": h,
        "rh_max1": max(recent1),
        "rh_max3": max(recent3),
        "sat3": sum(1 for v in recent3 if v >= SATURATED_HUMIDITY) / len(recent3),
        "temp": t,
        "td": dew,
        "spread": t - dew,
        "dT1": change(1, 0),
        "dT3": change(3, 0),
        "dT24": change(24, 0),
        "dtd3": None if then3 is None else dew - compute_dew_point(then3[0], then3[1]),
        "drh3": change(3, 1),
        "pct30": rank(p, long_rank),
        "pct7": None if short_rank is None else rank(p, short_rank),
        "dp1": change(1, 2),
        "dp3": change(3, 2),
        "dp6": change(6, 2),
        "dp12": change(12, 2),
        "t_range3": w.swing(3, series.temperature),
        "t_range6": w.swing(6, series.temperature),
        "t_range12": w.swing(12, series.temperature),
        "t_range24": w.swing(24, series.temperature),
        "rh_range6": w.swing(6, series.humidity),
        "rh_min24": (
            None if (low := w.covering(24, series.humidity)) is None else min(low)
        ),
        "hour": now.hour,
    }
    return {
        name: None if value is None or math.isnan(value) else round(value, DECIMALS[name])
        for name, value in values.items()
    }
