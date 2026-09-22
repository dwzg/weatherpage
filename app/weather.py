"""Derived meteorological values.

Everything here is a pure function of its arguments — no database, no clock
except what the caller passes in — so it can be reasoned about and tested in
isolation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

# ── Thresholds ─────────────────────────────────────────────────────────────
# Named rather than inlined so the forecast rules below read as prose.

#: Dew-point spread under which the air is close enough to saturation for
#: fog or drizzle. It is NOT a rain signal: on a clear, calm night the
#: temperature falls to the dew point and this closes every time, which is
#: dew forming on the balcony rather than rain on the way.
SATURATION_SPREAD_C = 3.0

#: Humidity above which the air is damp enough to rain out of.
HUMIDITY_WET = 70.0
#: Humidity at which the air itself is the story.
HUMIDITY_MUGGY = 85.0

#: A warm, humid summer afternoon, when a shower can build with no warning
#: from the barometer at all.
CONVECTIVE_TEMP_C = 25.0
CONVECTIVE_HUMIDITY = 50.0
CONVECTIVE_HOURS = range(12, 19)
CONVECTIVE_MONTHS = frozenset({4, 5, 6, 7, 8, 9})

#: A trend delta larger than this counts as a real rise or fall.
TREND_SIGNIFICANT = 1.0

#: Below this temperature the plants on the balcony are at risk.
FROST_WARNING_C = 2.0

#: And below this, with the temperature still falling, they are about to be.
#: A warning that first appears at 2 °C is a warning you read while it is
#: already happening; on a clear night the air keeps dropping for hours after
#: it passes 4 °C, so this is the one with time to act on it left in it.
FROST_WATCH_C = 4.0

#: What :func:`frost_alert` answers with. Identifiers, like the forecast
#: phrases: the API stays English and the page translates them.
FROST_NOW = "Frost"
FROST_SOON = "Frost likely"

#: The heat index is only meaningful in warm air; below this it is not shown.
HEAT_INDEX_MIN_C = 27.0

NO_DATA = "Not enough data"


def compute_dew_point(temp_c: float, humidity: float) -> float:
    """Dew point in °C via the Magnus formula."""
    a, b = 17.27, 237.7
    gamma = (a * temp_c) / (b + temp_c) + math.log(max(humidity, 1e-6) / 100.0)
    return round((b * gamma) / (a - gamma), 1)


def compute_heat_index(temp_c: float, humidity: float) -> float | None:
    """Apparent ("feels like") temperature in °C, or ``None`` when not meaningful.

    Implements NOAA's Rothfusz regression including the two correction terms
    for very dry and very humid air, which the regression alone gets wrong.
    Returns ``None`` below :data:`HEAT_INDEX_MIN_C`, where the heat index is
    not defined.
    """
    if temp_c < HEAT_INDEX_MIN_C:
        return None

    t = temp_c * 9 / 5 + 32  # Rothfusz is defined in Fahrenheit
    rh = humidity

    hi = (
        -42.379
        + 2.04901523 * t
        + 10.14333127 * rh
        - 0.22475541 * t * rh
        - 6.83783e-3 * t * t
        - 5.481717e-2 * rh * rh
        + 1.22874e-3 * t * t * rh
        + 8.5282e-4 * t * rh * rh
        - 1.99e-6 * t * t * rh * rh
    )

    # NOAA adjustments outside the regression's comfortable range.
    if rh < 13 and 80 <= t <= 112:
        hi -= ((13 - rh) / 4) * math.sqrt((17 - abs(t - 95)) / 17)
    elif rh > 85 and 80 <= t <= 87:
        hi += ((rh - 85) / 10) * ((87 - t) / 5)

    return round((hi - 32) * 5 / 9, 1)


def frost_alert(temp_c: float, direction: str | None = None) -> str | None:
    """Frost now, frost coming, or nothing.

    Two stages because they are two different pieces of news. Below
    :data:`FROST_WARNING_C` is a statement about the reading on the card, so
    it reads the reading itself. The watch is a claim about the next few
    hours, so it reads the smoothed 3-hour trend instead: a single cold
    sample is not a night getting colder.
    """
    if temp_c < FROST_WARNING_C:
        return FROST_NOW
    if temp_c < FROST_WATCH_C and direction == "falling":
        return FROST_SOON
    return None


# ── Forecast ───────────────────────────────────────────────────────────────
#
# These rules were fitted against 90 days of this station's readings scored
# on observed hourly rainfall for the station's own location. That reference
# data was used offline, for calibration only: the app itself fetches nothing
# and forecasts from its own sensor. What the scoring showed:
#
#   * The barometric tendency every weather-lore rule reaches for first has
#     NEGATIVE skill here. "Pressure fell more than 1 hPa in 6 hours" scored
#     a Hanssen-Kuipers score of -0.09 against rain within six hours, and
#     inside the wettest conditions rising pressure was followed by rain
#     more often than falling pressure (64% against 39%). The rules this
#     replaces were built entirely on tendency and scored -0.074: worse than
#     saying nothing at all.
#   * Where the pressure sits does carry signal, but no fixed threshold
#     transfers — "below 1020 hPa" scored a critical success index of 0.05 in
#     one month of the sample and 0.44 in another. Ranking the reading
#     against the station's own recent range is stable across all 90 days.
#   * Humidity is the second axis. Together the two separate conditions that
#     saw rain within six hours 6% of the time from ones that saw it 69% of
#     the time, against a 21% base rate.
#
# So the barometer is read as a level, not a tendency. The trend is still
# measured and shown next to the pressure card, because it is a fact about
# the last six hours; it just is not evidence about the next six.

#: Pressure percentile and humidity bounding each tier. Observed
#: rain-within-6h over the 90-day sample, against a 21% base rate:
#: rain likely 69%, rain possible 32%, unsettled 28%, changeable 15%,
#: settled 6%.
RAIN_LIKELY_PERCENTILE = 0.25
RAIN_LIKELY_HUMIDITY = 80.0
RAIN_POSSIBLE_PERCENTILE = 0.40
RAIN_POSSIBLE_HUMIDITY = 65.0
SETTLED_PERCENTILE = 0.60

# ── What the calibration measured ──────────────────────────────────────────
# These are the numbers the tiers above were chosen against, kept here as
# constants because the page prints them: a threshold that moves without its
# measured hit rate moving with it is how an explainer starts lying.

#: How often rain followed within six hours over the whole 90-day sample.
CALIBRATION_BASE_RATE = 0.21
#: How many days of this station's readings the tiers were scored on.
CALIBRATION_DAYS = 90
#: The binary "says rain" forecast, scored over that sample.
CALIBRATION_CSI = 0.27
CALIBRATION_KSS = 0.27
#: What the tendency-based rules this replaced scored, same sample. Negative:
#: worse than saying nothing.
SUPERSEDED_KSS = -0.074
#: The barometric tendency on its own, as a rain signal. Also negative.
TENDENCY_KSS = -0.09
#: Inside the wettest conditions, how often rain followed a *rising* against a
#: *falling* barometer. The wrong way round, which is the whole argument for
#: reading pressure as a level.
TENDENCY_WET_RISING = 0.64
TENDENCY_WET_FALLING = 0.39
#: A fixed hPa threshold ("below 1020") scored this in the worst and best
#: month of the sample. The spread is why the reading is ranked instead.
FIXED_THRESHOLD_CSI_RANGE = (0.05, 0.44)


#: The same figures gathered for the page. A view of the constants above, not
#: a second copy of them: the explainer prints what the rules were fitted to.
CALIBRATION: dict[str, float] = {
    "days": CALIBRATION_DAYS,
    "base_rate": CALIBRATION_BASE_RATE,
    "csi": CALIBRATION_CSI,
    "kss": CALIBRATION_KSS,
    "superseded_kss": SUPERSEDED_KSS,
    "tendency_kss": TENDENCY_KSS,
    "wet_rising": TENDENCY_WET_RISING,
    "wet_falling": TENDENCY_WET_FALLING,
    "fixed_csi_low": FIXED_THRESHOLD_CSI_RANGE[0],
    "fixed_csi_high": FIXED_THRESHOLD_CSI_RANGE[1],
}


@dataclass(frozen=True)
class Tier:
    """One rung of the ladder :func:`compute_forecast` walks, for display.

    This is documentation that the page renders, not something the rules
    consult — :func:`compute_forecast` below is still the implementation, and
    reads the same constants. Keeping the two beside each other is the point:
    a tier that is retuned without its row being retuned is visible here.
    """

    phrase: str                 #: the forecast phrase this rung produces
    condition: str              #: message id describing when it fires
    fields: dict                #: placeholder values for that message
    observed: float | None      #: measured rain-within-6h, or None
    note: str = ""              #: shown instead, when nothing cleaner was measured


#: The tiers in the order :func:`compute_forecast` tests them. The observed
#: rates are that function's own calibration: 69% at the top down to 6% at the
#: bottom, against a 21% base rate.
RULE_LADDER: tuple[Tier, ...] = (
    Tier(
        "Rain likely",
        "Pressure in the lowest {pct}% of 30 days, humidity above {rh}%",
        {"pct": RAIN_LIKELY_PERCENTILE * 100, "rh": RAIN_LIKELY_HUMIDITY},
        0.69,
    ),
    Tier(
        "Thunderstorm possible",
        "Above {temp}°C and humidity above {rh}%, {start}:00 to {end}:59, {first} to {last}",
        {
            "temp": CONVECTIVE_TEMP_C,
            "rh": CONVECTIVE_HUMIDITY,
            "start": min(CONVECTIVE_HOURS),
            "end": max(CONVECTIVE_HOURS),
            "month_first": min(CONVECTIVE_MONTHS),
            "month_last": max(CONVECTIVE_MONTHS),
        },
        None,
        note="about twice the base rate, whatever the barometer says",
    ),
    Tier(
        "Rain possible",
        "Pressure in the lowest {pct}% of 30 days, humidity above {rh}%",
        {"pct": RAIN_POSSIBLE_PERCENTILE * 100, "rh": RAIN_POSSIBLE_HUMIDITY},
        0.32,
    ),
    Tier(
        "Unsettled",
        "Pressure in the lowest {pct}% of 30 days, drier than that",
        {"pct": RAIN_POSSIBLE_PERCENTILE * 100},
        0.28,
    ),
    Tier(
        "Little change",
        "Pressure in the middle of its range, {low}% to {high}%",
        {"low": RAIN_POSSIBLE_PERCENTILE * 100, "high": SETTLED_PERCENTILE * 100},
        0.15,
    ),
    Tier(
        "Fair and settled",
        "Pressure above the {pct}th percentile, humidity below {rh}%",
        {"pct": SETTLED_PERCENTILE * 100, "rh": HUMIDITY_WET},
        0.06,
    ),
)


@dataclass(frozen=True)
class ForecastInputs:
    """Everything the rules below consult, derived once from raw readings."""

    percentile: float | None  # where the pressure sits in its recent range
    humidity: float
    near_saturation: bool
    humidity_rising: bool
    convective: bool

    @classmethod
    def build(
        cls,
        percentile: float | None,
        humidity: float,
        temperature: float | None,
        dew_point: float | None,
        humidity_trend: dict | None,
        moment: datetime,
    ) -> ForecastInputs:
        spread = (
            temperature - dew_point
            if temperature is not None and dew_point is not None
            else None
        )

        return cls(
            percentile=percentile,
            humidity=humidity,
            near_saturation=spread is not None and spread < SATURATION_SPREAD_C,
            humidity_rising=_is_rising(humidity_trend),
            convective=(
                temperature is not None
                and temperature > CONVECTIVE_TEMP_C
                and humidity > CONVECTIVE_HUMIDITY
                and moment.hour in CONVECTIVE_HOURS
                and moment.month in CONVECTIVE_MONTHS
            ),
        )


def _is_rising(trend: dict | None) -> bool:
    return trend is not None and trend["delta"] > TREND_SIGNIFICANT


def _air_now(f: ForecastInputs) -> str:
    """What the air is doing, for when the barometer has nothing to add."""
    if f.near_saturation and f.humidity_rising:
        return "Fog or drizzle possible"
    if f.humidity > HUMIDITY_MUGGY:
        return "Overcast and humid"
    return "Little change"


def compute_forecast(
    pressure_percentile: float | None,
    humidity: float,
    temperature: float | None = None,
    dew_point: float | None = None,
    humidity_trend: dict | None = None,
    moment: datetime | None = None,
) -> str:
    """A short outlook from where the pressure sits and how humid the air is.

    ``pressure_percentile`` is the current pressure's rank within the
    station's recent range, from :func:`database.get_pressure_percentile`;
    ``None`` means there is not enough history to rank against yet, and the
    phrase then describes the air rather than predicting anything.

    This is one sensor on a balcony, with no wind and nothing upstream, so
    it distinguishes settling from deteriorating and declines to be more
    specific. ``moment`` defaults to the current local time and exists so
    tests can pin the clock.
    """
    if moment is None:
        from .clock import now

        moment = now()

    f = ForecastInputs.build(
        pressure_percentile, humidity, temperature, dew_point, humidity_trend, moment
    )

    if f.percentile is None:
        return _air_now(f)

    if f.percentile < RAIN_LIKELY_PERCENTILE and f.humidity > RAIN_LIKELY_HUMIDITY:
        return "Rain likely"

    # Warm, humid summer afternoons carry their own risk, whatever the
    # barometer says: twice the base rate of rain over the sample.
    if f.convective:
        return "Thunderstorm possible"

    if f.percentile < RAIN_POSSIBLE_PERCENTILE:
        if f.humidity > RAIN_POSSIBLE_HUMIDITY:
            return "Rain possible"
        if f.near_saturation and f.humidity_rising:
            return "Fog or drizzle possible"
        return "Unsettled"

    if f.percentile > SETTLED_PERCENTILE:
        if f.near_saturation and f.humidity_rising:
            return "Fog or drizzle possible"
        if f.humidity > HUMIDITY_MUGGY:
            return "Overcast and humid"
        if f.humidity < HUMIDITY_WET:
            return "Fair and settled"
        return "Settled but humid"

    return _air_now(f)


# ── Composing the outlook from the two fitted models ───────────────────────
#
# The ladder above reads raw sensor values through thresholds fitted by hand.
# Where a model exists, the same ladder reads its probability instead, and the
# rung stops being an assertion and starts being a measurement.
#
# Only two of the axes have ground truth to fit against, and that is not a
# choice — it is what the label sources actually contain. Measured over two
# years of the reanalysis at the station's own location:
#
#   * Rain has labels (observed precipitation) and a model: app/model.json.
#   * Cloud has labels (observed cloud cover) and a model: app/sky_model.json.
#     It replaces the humidity-only guess behind "Fair and settled" and
#     "Overcast and humid" — the one claim on the banner with nothing
#     measured behind it.
#   * Fog has NO label. The archive emits its fog codes (45, 48) exactly zero
#     times in 17,520 hours, at a valley site where fog is common. That is a
#     property of how the reanalysis derives the code, not of the weather.
#   * Thunderstorms have NO label either: zero occurrences of codes 95-99,
#     and the archive's CAPE field comes back empty.
#
# So those two rungs stay hand-made. A model cannot be fitted to a label that
# does not exist, and pretending otherwise would put a learned-looking number
# on a guess.

#: The three states the sky model's probability is read as. Strings rather
#: than an enum because they are compared in one place and printed in none.
SKY_OVERCAST = "overcast"
SKY_MIXED = "mixed"
SKY_CLEAR = "clear"

#: Where the bands are cut. Unlike the rain model's threshold — which
#: training fits by maximising CSI and writes into model.json — these are
#: chosen by hand, and they can be, because the probability is calibrated:
#: at 0.60 roughly three in five such hours really are overcast. A middle
#: band exists so that "the model is unsure" and "the sky is genuinely
#: mixed" are not reported as the same thing.
SKY_OVERCAST_PROBABILITY = 0.60
SKY_CLEAR_PROBABILITY = 0.30


def sky_band(probability: float) -> str:
    """Which of the three sky states a probability falls in."""
    if probability >= SKY_OVERCAST_PROBABILITY:
        return SKY_OVERCAST
    if probability < SKY_CLEAR_PROBABILITY:
        return SKY_CLEAR
    return SKY_MIXED


def _sky_without_a_model(f: ForecastInputs) -> str:
    """The sky rungs as the ladder has always read them, from pressure and humidity.

    Reached when no sky model has shipped — which, so far, is always: the
    candidate has to clear its gates, and on a short archive it correctly
    refuses. The rain rungs above have already declined and fog has been
    ruled out, so what is left is "what is the air doing", which is exactly
    the question :func:`compute_forecast` answers here. Same answers as that
    function gives, in the same order, minus the rain rungs the model now owns.
    """
    if f.percentile is not None and f.percentile < RAIN_POSSIBLE_PERCENTILE:
        return "Unsettled"
    if f.humidity > HUMIDITY_MUGGY:
        return "Overcast and humid"
    if f.percentile is not None and f.percentile > SETTLED_PERCENTILE:
        return "Fair and settled" if f.humidity < HUMIDITY_WET else "Settled but humid"
    return "Little change"


def compose_forecast(
    rain_probability: float,
    rain_threshold: float,
    sky_probability: float | None,
    humidity: float,
    temperature: float | None = None,
    dew_point: float | None = None,
    humidity_trend: dict | None = None,
    moment: datetime | None = None,
    *,
    pressure_percentile: float | None = None,
) -> str:
    """The outlook, composed from both fitted models plus the unlabelled rungs.

    Used in place of :func:`compute_forecast` whenever the **rain** model is
    loaded and the feature vector is complete. ``sky_probability`` may be
    ``None``, and normally is: the sky model ships only once a candidate
    clears its gates, which on a short archive it correctly refuses to do.
    The sky rungs then fall back to :func:`_sky_without_a_model`, the
    pressure-and-humidity thresholds the ladder has always used there.

    Gating the whole thing on *both* models was a mistake worth naming. It
    meant that until the sky model shipped, the banner came from the
    threshold ladder while the pill beside it came from the rain model — two
    answers to one question, from the same page, disagreeing in public
    ("Rain possible" next to "Rain nearby not expected · 9%"). The page's own
    skill table says which to believe: the model beats the ladder on Brier,
    AUC, CSI and KSS alike. The rain rungs need only the rain model, so they
    now use it.

    The rain bands are :func:`app.nowcast.describe`'s, so the phrase on the
    banner and the word printed beside the percentage are the same decision
    read twice and cannot drift into contradicting each other.
    """
    if moment is None:
        from .clock import now

        moment = now()

    from .nowcast import describe

    f = ForecastInputs.build(
        pressure_percentile, humidity, temperature, dew_point, humidity_trend, moment
    )
    rain = describe(rain_probability, rain_threshold)

    if rain == "likely":
        return "Rain likely"

    # Hand-made, and staying that way: a warm humid afternoon carries a risk
    # the barometer cannot see, and no label source scores it.
    if f.convective:
        return "Thunderstorm possible"

    if rain == "possible":
        return "Rain possible"

    # Also hand-made, for the same reason. Tested before the sky rungs
    # because saturated air that is still wetting is the more specific claim.
    if f.near_saturation and f.humidity_rising:
        return "Fog or drizzle possible"

    if sky_probability is None:
        return _sky_without_a_model(f)

    band = sky_band(sky_probability)
    if band == SKY_OVERCAST:
        return "Overcast and humid" if f.humidity > HUMIDITY_MUGGY else "Cloudy"
    if band == SKY_CLEAR:
        return "Fair and settled" if f.humidity < HUMIDITY_WET else "Settled but humid"
    return "Little change"


def learned_ladder(rain_threshold: float, *, sky: bool = True) -> tuple[Tier, ...]:
    """The composed ladder, for the explainer to print.

    A function rather than a constant because its cut points are not
    constants: the rain threshold is fitted and arrives in model.json, and
    whether the sky rungs are a model or a threshold depends on whether a sky
    model has shipped. Same contract as :data:`RULE_LADDER` — generated from
    the numbers :func:`compose_forecast` actually reads, so retuning a band
    moves the page with it, and ``tests/test_weather.py`` drives the function
    to check the two still agree.

    ``sky=False`` is the live case today and prints the pressure-and-humidity
    rungs, because that is the reasoning the page did. Printing the model
    bands while the thresholds ran would be the explainer describing a
    calculation that did not happen.
    """
    sky_rungs = (
        (
            Tier(
                "Cloudy",
                "Sky model above {pct}%",
                {"pct": round(SKY_OVERCAST_PROBABILITY * 100)},
                None,
                note="fitted against observed cloud cover",
            ),
            Tier(
                "Little change",
                "Sky model between {low}% and {high}%",
                {
                    "low": round(SKY_CLEAR_PROBABILITY * 100),
                    "high": round(SKY_OVERCAST_PROBABILITY * 100),
                },
                None,
                note="fitted against observed cloud cover",
            ),
            Tier(
                "Fair and settled",
                "Sky model below {pct}%, humidity below {rh}%",
                {"pct": round(SKY_CLEAR_PROBABILITY * 100), "rh": HUMIDITY_WET},
                None,
                note="fitted against observed cloud cover",
            ),
        )
        if sky
        else (
            Tier(
                "Unsettled",
                "Pressure in the lowest {pct}% of 30 days",
                {"pct": RAIN_POSSIBLE_PERCENTILE * 100},
                None,
                note="threshold: no sky model has cleared its gates yet",
            ),
            Tier(
                "Overcast and humid",
                "Humidity above {rh}%",
                {"rh": HUMIDITY_MUGGY},
                None,
                note="threshold: no sky model has cleared its gates yet",
            ),
            Tier(
                "Fair and settled",
                "Pressure above the {pct}th percentile, humidity below {rh}%",
                {"pct": SETTLED_PERCENTILE * 100, "rh": HUMIDITY_WET},
                None,
                note="threshold: no sky model has cleared its gates yet",
            ),
            Tier(
                "Little change",
                "Pressure in the middle of its range, {low}% to {high}%",
                {"low": RAIN_POSSIBLE_PERCENTILE * 100, "high": SETTLED_PERCENTILE * 100},
                None,
                note="threshold: no sky model has cleared its gates yet",
            ),
        )
    )
    return (
        Tier(
            "Rain likely",
            "Rain model above {pct}%",
            {"pct": round(max(rain_threshold, 0.6) * 100)},
            None,
            note="fitted against observed rainfall",
        ),
        Tier(
            "Thunderstorm possible",
            "Above {temp}°C and humidity above {rh}%, {start}:00 to {end}:59",
            {
                "temp": CONVECTIVE_TEMP_C,
                "rh": CONVECTIVE_HUMIDITY,
                "start": min(CONVECTIVE_HOURS),
                "end": max(CONVECTIVE_HOURS),
            },
            None,
            note="hand-made: no label source scores thunderstorms here",
        ),
        Tier(
            "Rain possible",
            "Rain model above {pct}%",
            {"pct": round(rain_threshold * 100)},
            None,
            note="fitted against observed rainfall",
        ),
        Tier(
            "Fog or drizzle possible",
            "Dew-point spread under {spread}°C and humidity rising",
            {"spread": SATURATION_SPREAD_C},
            None,
            note="hand-made: the archive records no fog at all",
        ),
        *sky_rungs,
    )


#: Forecast phrase fragments mapped to the emoji shown beside them.
#:
#: Order matters and matching is case-insensitive, which the original rules
#: got wrong in both directions: "settled" is a substring of "unsettled", so
#: deteriorating forecasts were given a sun, and fragments were matched
#: case-sensitively, so "Low pressure, rain risk" missed the rain rule. The
#: worsening group is therefore tested before the improving one, and every
#: fragment here is lowercase.
_FORECAST_EMOJI: tuple[tuple[tuple[str, ...], str], ...] = (
    (("storm", "thunder"), "⛈️"),
    (("rain",), "🌧️"),
    (("fog", "drizzle"), "🌫️"),
    (("unsettled", "overcast", "humid", "cloud"), "☁️"),
    (("clearing", "improv", "fair", "settled"), "☀️"),
)

DEFAULT_FORECAST_EMOJI = "🌤️"


def forecast_emoji(forecast: str | None) -> str:
    """Pick the emoji that goes with a forecast phrase."""
    if not forecast:
        return DEFAULT_FORECAST_EMOJI
    lowered = forecast.lower()
    for fragments, emoji in _FORECAST_EMOJI:
        if any(fragment in lowered for fragment in fragments):
            return emoji
    return DEFAULT_FORECAST_EMOJI
