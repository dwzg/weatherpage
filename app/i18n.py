"""English and German for the dashboard.

The catalogue is keyed by the English source string, gettext-style. That is
deliberate rather than lazy: it keeps English working with an empty
catalogue, and it means the forecast phrases in :mod:`app.weather` stay
canonical English everywhere else in the system — the JSON API, the emoji
matching in :func:`app.weather.forecast_emoji`, the tests and ``ml/train.py``
all keep comparing the strings they always compared. Translation happens at
the edge, when something is about to be shown to a person.

There are two edges, because the page is server-rendered and then maintained
by the poller. Both read *this* catalogue: Jinja through :func:`translator`,
and the browser through the JSON blob :func:`page_payload` embeds, which
``static/js/i18n.js`` unpacks. One table, two consumers — the same
arrangement the forecast emoji already uses, and for the same reason.
"""

from __future__ import annotations

from collections.abc import Callable

#: The two languages the page speaks. Anything else negotiates down to English.
LANGUAGES = ("en", "de")
DEFAULT_LANGUAGE = "en"

#: Passed to ``Intl`` in the browser. ``en-GB`` rather than ``en-US`` so that
#: English keeps the 24-hour clock and day-month order the page has always had.
LOCALES = {"en": "en-GB", "de": "de-DE"}

MONTHS_LONG = {
    "en": ("January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"),
    "de": ("Januar", "Februar", "März", "April", "Mai", "Juni",
           "Juli", "August", "September", "Oktober", "November", "Dezember"),
}

MONTHS_SHORT = {
    "en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
    "de": ("Jan", "Feb", "Mär", "Apr", "Mai", "Jun",
           "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"),
}

#: Monday-first, matching the calendar layout.
DAYS_SHORT = {
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    "de": ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"),
}

#: Decimal and thousands separators. German writes 22,5 °C and 25.783 readings.
SEPARATORS = {"en": (".", ","), "de": (",", ".")}

GERMAN: dict[str, str] = {
    # ── Page chrome ────────────────────────────────────────────────────────
    "Balcony Weather Station": "Balkon-Wetterstation",
    "Balcony Weather": "Balkon-Wetter",
    "Live from the balcony": "Live vom Balkon",
    "Live temperature, humidity and pressure from a balcony weather station.":
        "Live-Temperatur, -Luftfeuchtigkeit und -Luftdruck einer Balkon-Wetterstation.",

    # ── Forecast phrases (app/weather.py) ──────────────────────────────────
    "Rain likely": "Regen wahrscheinlich",
    "Thunderstorm possible": "Gewitter möglich",
    "Rain possible": "Regen möglich",
    "Unsettled": "Wechselhaft",
    "Fog or drizzle possible": "Nebel oder Nieselregen möglich",
    "Settled but humid": "Beständig, aber schwül",
    "Overcast and humid": "Bedeckt und schwül",
    "Fair and settled": "Heiter und beständig",
    "Little change": "Wenig Veränderung",
    "Not enough data": "Nicht genug Daten",

    # ── Nowcast (app/nowcast.py describe()) ────────────────────────────────
    "likely": "wahrscheinlich",
    "possible": "möglich",
    "unlikely": "unwahrscheinlich",
    "not expected": "nicht zu erwarten",
    "Rain nearby": "Regen in der Umgebung",
    "in {hours} h": "in {hours} Std.",
    "Learned from this station's own history":
        "Aus der eigenen Messreihe dieser Station gelernt",

    # ── Alert banners ──────────────────────────────────────────────────────
    "❄️ Frost warning — protect your plants!":
        "❄️ Frostwarnung — Pflanzen schützen!",
    "⚠️ No new readings — the sensor feed may be down":
        "⚠️ Keine neuen Messwerte — der Sensor meldet sich möglicherweise nicht",

    # ── The explainer ──────────────────────────────────────────────────────
    "How these two predictions work": "Wie diese beiden Vorhersagen funktionieren",
    "☀️ The outlook": "☀️ Die Aussicht",
    "Thresholds on two numbers: where the pressure sits in this station's own "
    "last 30 days, and the humidity. It ignores which way the barometer is "
    "moving — measured against real rainfall here, that carries no signal.":
        "Schwellenwerte auf zwei Zahlen: wo der Luftdruck innerhalb der letzten "
        "30 Tage dieser Station liegt, und die Luftfeuchtigkeit. In welche "
        "Richtung sich das Barometer bewegt, bleibt unberücksichtigt — am "
        "tatsächlichen Niederschlag hier gemessen trägt das keine Information.",
    "🤖 The rain chance": "🤖 Die Regenwahrscheinlichkeit",
    "A model fitted to this station's readings against observed rainfall and "
    "retrained weekly. Ten measurements in, one number out: the chance of "
    "{mm} mm of rain within {hours} hours":
        "Ein Modell, das auf die Messwerte dieser Station gegen beobachteten "
        "Niederschlag angepasst und wöchentlich neu trainiert wird. Zehn "
        "Messwerte hinein, eine Zahl heraus: die Wahrscheinlichkeit für "
        "{mm} mm Regen innerhalb von {hours} Stunden",
    "in the surrounding area": "in der Umgebung",
    "— not on this balcony. Whether a shower crosses here or the next valley "
    "is not something a barometer knows.":
        "— nicht auf diesem Balkon. Ob ein Schauer hier oder im nächsten Tal "
        "niedergeht, weiß ein Barometer nicht.",
    "Why both": "Warum beides",
    "The phrase is a category; the percentage is a calibrated probability, and "
    "they can disagree. Neither sees wind, radar or anything upstream of the "
    "balcony, so treat both as a hint rather than a forecast.":
        "Der Text ist eine Kategorie, der Prozentwert eine kalibrierte "
        "Wahrscheinlichkeit — beide können sich widersprechen. Keine von "
        "beiden sieht Wind, Radar oder sonst etwas oberhalb des Balkons; sie "
        "sind daher eher ein Hinweis als eine Vorhersage.",
    "Model trained {date}": "Modell trainiert am {date}",
    "{bss}% skill over climatology": "{bss} % Güte gegenüber der Klimatologie",

    # ── Current conditions ─────────────────────────────────────────────────
    "Temperature": "Temperatur",
    "Humidity": "Luftfeuchtigkeit",
    "Pressure": "Luftdruck",
    "Feels like {value}°C": "Gefühlt {value} °C",
    "Dew point {value}°C": "Taupunkt {value} °C",
    "vs {hours}h ago: {delta}°C": "vs. vor {hours} h: {delta} °C",
    "vs {hours}h ago: {delta}%": "vs. vor {hours} h: {delta} %",
    "Last updated: {timestamp}": "Zuletzt aktualisiert: {timestamp}",

    # ── Charts ─────────────────────────────────────────────────────────────
    "Chart period": "Diagrammzeitraum",
    "24 Hours": "24 Stunden",
    "7 Days": "7 Tage",
    "30 Days": "30 Tage",
    "All Time": "Gesamt",
    "Min": "Min",
    "Max": "Max",
    "Average": "Mittel",
    "Charts need network access to load the charting library.":
        "Die Diagramme brauchen Netzzugriff, um die Diagrammbibliothek zu laden.",
    "Averaged into {interval} intervals · shaded band shows the range within each":
        "Gemittelt über {interval} · das schattierte Band zeigt die Spanne je Intervall",
    "{n}-day": "{n}-Tages-Intervalle",
    "{n}-hour": "{n}-Stunden-Intervalle",
    "{n}-minute": "{n}-Minuten-Intervalle",

    # ── Records ────────────────────────────────────────────────────────────
    "📅 Today's Records": "📅 Rekorde heute",
    "🏆 All-Time Records": "🏆 Rekorde insgesamt",
    "Hottest": "Am wärmsten",
    "Coldest": "Am kältesten",
    "Most humid": "Am feuchtesten",
    "Least humid": "Am trockensten",
    "Highest pressure": "Höchster Luftdruck",
    "Lowest pressure": "Niedrigster Luftdruck",
    "Warmest day (avg)": "Wärmster Tag (Ø)",
    "Coldest day (avg)": "Kältester Tag (Ø)",
    "Most humid day (avg)": "Feuchtester Tag (Ø)",
    "Least humid day (avg)": "Trockenster Tag (Ø)",
    "Readings": "Messwerte",
    "at {time}": "um {time}",

    # ── Climate ────────────────────────────────────────────────────────────
    "Climate": "Klima",
    "Monthly Details": "Monatsdetails",
    "Year": "Jahr",
    "{year} average": "Mittel {year}",
    "min {min} max {max}": "min {min} max {max}",

    # ── Calendar ───────────────────────────────────────────────────────────
    "Temperature Calendar": "Temperaturkalender",
    "Previous month": "Vorheriger Monat",
    "Next month": "Nächster Monat",
    "Temperature calendar, one month per page":
        "Temperaturkalender, ein Monat pro Seite",
    "Cold": "Kalt",
    "Hot": "Heiß",
    "Avg": "Ø",
    "Low": "Tief",
    "High": "Hoch",
    "No data": "Keine Daten",
    "{count} day with data": "{count} Tag mit Daten",
    "{count} days with data": "{count} Tage mit Daten",
    "No readings this month": "Keine Messwerte in diesem Monat",
    "Not enough data yet": "Noch nicht genug Daten",

    # ── Empty state ────────────────────────────────────────────────────────
    "📡 Waiting for first weather reading...": "📡 Warte auf den ersten Messwert …",
    "Data arrives every few minutes from the balcony sensor.":
        "Alle paar Minuten treffen neue Daten vom Balkonsensor ein.",
}

#: English needs no table: the source string *is* the message id, so a lookup
#: that misses falls through to exactly the right text.
CATALOGUES: dict[str, dict[str, str]] = {"en": {}, "de": GERMAN}


def negotiate(header: str | None, override: str | None = None) -> str:
    """Pick a language from an ``Accept-Language`` header.

    ``override`` wins when it names a language we speak; it is what ``?lang=``
    on the URL sets, so the page can be read in the other language without
    changing an OS setting, and so tests can ask for one directly.

    Only the primary subtag matters — ``de-AT`` and ``de-CH`` are German here.
    Quality values are honoured, so a browser configured for French first and
    German second gets German rather than the English fallback.
    """
    if override and (tag := _primary(override)) in LANGUAGES:
        return tag
    if not header:
        return DEFAULT_LANGUAGE

    best: tuple[float, int] | None = None
    chosen = DEFAULT_LANGUAGE
    for position, part in enumerate(header.split(",")):
        tag, _, params = part.strip().partition(";")
        language = _primary(tag)
        if language not in LANGUAGES:
            continue
        quality = _quality(params)
        if quality <= 0.0:  # "q=0" means "explicitly not this one".
            continue
        # Earlier entries win ties, which is how the header is ordered anyway.
        rank = (quality, -position)
        if best is None or rank > best:
            best, chosen = rank, language
    return chosen


def _primary(tag: str) -> str:
    return tag.strip().lower().partition("-")[0]


def _quality(params: str) -> float:
    for param in params.split(";"):
        key, _, value = param.partition("=")
        if key.strip().lower() == "q":
            try:
                return float(value)
            except ValueError:
                return 0.0
    return 1.0


def translator(lang: str) -> Callable[..., str]:
    """A ``t(text, **fields)`` bound to one language, for the template context.

    Placeholders are named and filled with :meth:`str.format`, so a German
    sentence may reorder them — which several of these do.
    """
    catalogue = CATALOGUES.get(lang, {})

    def t(text: str, **fields: object) -> str:
        translated = catalogue.get(text, text)
        return translated.format(**fields) if fields else translated

    return t


def number_formatter(lang: str) -> Callable[..., str]:
    """A ``num(value, digits)`` bound to one language, for the template context."""

    def num(value: float | int | None, digits: int = 0, *, sign: bool = False,
            grouping: bool = False) -> str:
        return format_number(value, digits, lang, sign=sign, grouping=grouping)

    return num


def format_number(value: float | int | None, digits: int = 0,
                  lang: str = DEFAULT_LANGUAGE, *, sign: bool = False,
                  grouping: bool = False) -> str:
    """Format a number the way the language writes it.

    Done by hand rather than through :mod:`locale`, which is process-global
    and would make the answer depend on what the container happens to have
    installed — the same reason this app formats timestamps itself.
    """
    if value is None:
        return ""
    decimal, thousands = SEPARATORS.get(lang, SEPARATORS[DEFAULT_LANGUAGE])
    text = f"{value:{'+' if sign else ''}{',' if grouping else ''}.{digits}f}"
    # Swap through a placeholder so a German thousands "." cannot be re-read
    # as the decimal separator on the second pass.
    return text.replace(",", "\0").replace(".", decimal).replace("\0", thousands)


def page_payload(lang: str) -> dict:
    """What the browser needs to speak the same language as the render.

    The whole catalogue goes over, not a hand-picked subset: a list of "these
    strings are the JavaScript ones" is a list that goes stale silently. It is
    a couple of kilobytes on a page that is already ``no-store``.
    """
    return {
        "lang": lang,
        "locale": LOCALES.get(lang, LOCALES[DEFAULT_LANGUAGE]),
        "decimal": SEPARATORS.get(lang, SEPARATORS[DEFAULT_LANGUAGE])[0],
        "thousands": SEPARATORS.get(lang, SEPARATORS[DEFAULT_LANGUAGE])[1],
        "strings": CATALOGUES.get(lang, {}),
        "months_long": list(MONTHS_LONG.get(lang, MONTHS_LONG[DEFAULT_LANGUAGE])),
        "months_short": list(MONTHS_SHORT.get(lang, MONTHS_SHORT[DEFAULT_LANGUAGE])),
        "days_short": list(DAYS_SHORT.get(lang, DAYS_SHORT[DEFAULT_LANGUAGE])),
    }
