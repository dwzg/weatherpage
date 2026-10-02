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
    "Language": "Sprache",
    "Live temperature, humidity and pressure from a balcony weather station.":
        "Live-Temperatur, -Luftfeuchtigkeit und -Luftdruck einer Balkon-Wetterstation.",

    # ── Forecast phrases (app/weather.py) ──────────────────────────────────
    "Rain likely": "Regen wahrscheinlich",
    "Thunderstorm possible": "Gewitter möglich",
    "Rain possible": "Regen möglich",
    "Unsettled": "Wechselhaft",
    "Fog or drizzle possible": "Nebel oder Nieselregen möglich",
    "Fog possible": "Nebel möglich",
    "Settled but humid": "Beständig, aber schwül",
    "Overcast and humid": "Bedeckt und schwül",
    "Fair and settled": "Heiter und beständig",
    "Cloudy": "Bewölkt",
    "Little change": "Wenig Veränderung",
    "Not enough data": "Nicht genug Daten",

    # ── Nowcast (app/nowcast.py describe()) ────────────────────────────────
    "likely": "wahrscheinlich",
    "possible": "möglich",
    "unlikely": "unwahrscheinlich",
    "not expected": "nicht zu erwarten",
    "Rain here": "Regen hier",
    "in {hours} h": "in {hours} Std.",
    # Suffix on each card's trend arrow: "+1.2 °C /3h".
    "/{hours}h": "/{hours} Std.",
    "Learned from years of weather-service observations":
        "Aus jahrelangen Beobachtungen des Wetterdienstes gelernt",

    # ── Alert banners ──────────────────────────────────────────────────────
    "❄️ Frost warning — protect your plants!":
        "❄️ Frostwarnung — Pflanzen schützen!",
    "❄️ Frost likely in a few hours — cover the plants while you can":
        "❄️ Frost in einigen Stunden wahrscheinlich — Pflanzen "
        "abdecken, solange es geht",
    "⚠️ No new readings — the sensor feed may be down":
        "⚠️ Keine neuen Messwerte — der Sensor meldet sich möglicherweise nicht",
    "⚠️ Not reachable — these readings may be out of date":
        "⚠️ Nicht erreichbar — diese Messwerte sind möglicherweise veraltet",

    # ── The explainer ──────────────────────────────────────────────────────
    "How these two predictions work": "Wie diese beiden Vorhersagen funktionieren",
    "☀️ The outlook": "☀️ Die Aussicht",
    "Thresholds on two numbers: where the pressure sits in this station's own "
    "last 30 days, and the humidity. It ignores which way the barometer is "
    "moving: as a threshold on its own, that scored worse than nothing here.":
        "Schwellenwerte auf zwei Zahlen: wo der Luftdruck innerhalb der letzten "
        "30 Tage dieser Station liegt, und die Luftfeuchtigkeit. In welche "
        "Richtung sich das Barometer bewegt, bleibt unberücksichtigt: als "
        "Schwellenwert für sich allein schnitt das hier schlechter ab als gar "
        "nichts.",
    "🤖 The rain chance": "🤖 Die Regenwahrscheinlichkeit",
    "A model trained on years of ten-minute readings from the German weather "
    "service's own stations — the same three things this balcony measures, "
    "each checked against the rain gauge standing beside it. It is retrained "
    "weekly, and a new fit only replaces it after passing a test on this "
    "balcony's own record. One number out: the chance of at least {mm} mm of "
    "rain":
        "Ein Modell, trainiert auf jahrelangen Zehn-Minuten-Messwerten der "
        "Stationen des Deutschen Wetterdienstes — dieselben drei Größen, die "
        "dieser Balkon misst, jeweils geprüft am Regenmesser, der daneben "
        "steht. Es wird wöchentlich neu trainiert, und eine neue Anpassung "
        "ersetzt es erst, nachdem sie an den eigenen Messwerten dieses "
        "Balkons eine Prüfung bestanden hat. Heraus kommt eine Zahl: die "
        "Wahrscheinlichkeit für mindestens {mm} mm Regen",
    "here, within {hours} hours": "hier, innerhalb von {hours} Stunden",
    "What it reads best is rain that has already begun: a wet sensor, a "
    "sudden chill, a jump in the barometer. A shower still on its way is "
    "beyond any balcony.":
        "Am besten erkennt es Regen, der schon eingesetzt hat: einen nassen "
        "Sensor, eine plötzliche Abkühlung, einen Sprung im Barometer. Ein "
        "Schauer, der noch unterwegs ist, bleibt jedem Balkon verborgen.",
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

    "One claim about the next six hours, picked from a ladder: thunder, "
    "rain, fog, cloud. Each rung reads its own model — fitted like the rain "
    "chance below, to what the weather service's stations observed — where "
    "that model has passed its tests, and a threshold on the pressure and "
    "the humidity where none has yet. The technical details say which is "
    "which.":
        "Eine Aussage über die nächsten sechs Stunden, gewählt von einer "
        "Leiter: Gewitter, Regen, Nebel, Bewölkung. Jede Sprosse liest ihr "
        "eigenes Modell — angepasst wie die Regenchance unten, an das, was "
        "die Stationen des Wetterdienstes beobachtet haben —, wo dieses "
        "Modell seine Prüfungen bestanden hat, und einen Schwellenwert für "
        "Luftdruck und Feuchte, wo noch keines das geschafft hat. Die "
        "technischen Details sagen, was davon gilt.",
    "The phrase names the one thing most worth knowing about the next six "
    "hours; the percentage is the chance of rain alone, and the rain rungs "
    "of the phrase read that same number. Neither sees wind, radar or "
    "anything upstream of the balcony, so treat both as a hint rather than "
    "a forecast.":
        "Der Text nennt das eine, was über die nächsten sechs Stunden am "
        "wissenswertesten ist; der Prozentwert ist allein die Regenchance, "
        "und die Regensprossen des Textes lesen genau diese Zahl. Keiner von "
        "beiden sieht Wind, Radar oder sonst etwas oberhalb des Balkons; sie "
        "sind daher eher ein Hinweis als eine Vorhersage.",

    # ── The deep dive ──────────────────────────────────────────────────────
    "The technical details": "Die technischen Details",

    # The outlook, in full
    "☀️ The outlook: a ladder of thresholds":
        "☀️ Die Aussicht: eine Leiter aus Schwellenwerten",
    "☀️ The outlook: a ladder of fitted models":
        "☀️ Die Aussicht: eine Leiter aus angepassten Modellen",
    "Tested in order, and the first rung that matches wins. The rain rungs "
    "read the fitted probability shown on the pill above — the same number, "
    "so the two cannot disagree. Every other rung reads its own model where "
    "one has cleared its gates, and the threshold it always had where none "
    "has yet; the right-hand column says which.":
        "Der Reihe nach geprüft; die erste zutreffende Sprosse gewinnt. Die "
        "Regensprossen lesen die angepasste Wahrscheinlichkeit, die oben auf "
        "der Plakette steht — dieselbe Zahl, die beiden können sich also "
        "nicht widersprechen. Jede andere Sprosse liest ihr eigenes Modell, "
        "wo eines seine Hürden genommen hat, und sonst den Schwellenwert, den "
        "sie schon immer hatte; die rechte Spalte sagt, was davon gilt.",

    # The other three models (sky, fog, thunder)
    "The other three models": "Die anderen drei Modelle",
    "Each is fitted like the rain model — the same trees, the same signals, "
    "the same weather-service stations — to what those stations observed: "
    "how much of the sky was cloud, how far one could see, and thunder their "
    "observers heard. Each must clear the same gates before it ships, and "
    "until it has, its rung keeps the threshold it always had.":
        "Jedes ist angepasst wie das Regenmodell — dieselben Bäume, dieselben "
        "Signale, dieselben Stationen des Wetterdienstes — an das, was diese "
        "Stationen beobachtet haben: wie viel des Himmels bewölkt war, wie "
        "weit man sehen konnte, und Donner, den ihre Beobachter hörten. Jedes "
        "muss dieselben Hürden nehmen, bevor es ausgeliefert wird, und bis "
        "dahin behält seine Sprosse den Schwellenwert, den sie schon immer "
        "hatte.",
    "Model": "Modell",
    "Held-out year": "Zurückgehaltenes Jahr",
    "This balcony": "Dieser Balkon",
    "Overcast: {pct}% cloud or more over the next {hours} h":
        "Bedeckt: {pct} % Bewölkung oder mehr in den nächsten {hours} Std.",
    "Fog: visibility under {metres} m within {hours} h":
        "Nebel: Sichtweite unter {metres} m innerhalb von {hours} Std.",
    "Thunder within {hours} h": "Gewitter innerhalb von {hours} Std.",
    "{bss} skill · AUC {auc}": "{bss} Skill · AUC {auc}",
    "the rule: AUC {auc}": "die Regel: AUC {auc}",
    "{bss} skill over {hours} h": "{bss} Skill über {hours} Std.",
    "level matched to the stations": "Niveau an die Stationen angeglichen",
    "not shipped yet: the hand-made rung runs":
        "noch nicht ausgeliefert: die Sprosse von Hand gilt",
    "Fog has to prove itself on this balcony before it ships, not only at "
    "the stations: a sensor that sits at 100% whenever it is wet looks, to a "
    "model fitted on ventilated screens, like exactly the saturated air fog "
    "forms in.":
        "Nebel muss sich auf diesem Balkon bewähren, bevor er ausgeliefert "
        "wird, nicht nur an den Stationen: Ein Sensor, der bei jeder Nässe "
        "auf 100 % steht, sieht für ein an belüfteten Wetterhütten "
        "angepasstes Modell genau wie die gesättigte Luft aus, in der Nebel "
        "entsteht.",
    "Thunder is labelled only up to {date}. The weather service's observers "
    "reported it until they went off duty in 2022, and the instruments that "
    "replaced them do not. So nothing near this balcony reports thunder now "
    "and the model cannot be scored here; instead, its level on these "
    "readings is matched to how often the stations had thunder in the same "
    "months.":
        "Gewitter sind nur bis {date} erfasst. Die Beobachter des "
        "Wetterdienstes meldeten sie, bis sie 2022 ihren Dienst beendeten, "
        "und die Instrumente, die sie ersetzten, tun es nicht. In der Nähe "
        "dieses Balkons meldet daher heute nichts mehr Gewitter, und das "
        "Modell kann hier nicht bewertet werden; stattdessen wird sein "
        "Niveau auf diesen Messwerten daran angeglichen, wie oft die "
        "Stationen in denselben Monaten Gewitter hatten.",
    # The threshold-sky rungs, printed while no cloud model has shipped.
    "Pressure in the lowest {pct}% of 30 days":
        "Luftdruck in den untersten {pct} % von 30 Tagen",
    "Humidity above {rh}%": "Feuchte über {rh} %",
    "threshold: no sky model has cleared its gates yet":
        "Schwellenwert: noch hat kein Wolkenmodell seine Hürden genommen",
    "Two inputs, tested in order. The first rung that matches wins, so the "
    "ladder reads top to bottom. Both inputs are medians over {minutes} "
    "minutes rather than the latest sample — the rungs sit close enough "
    "together that one noisy reading would otherwise flip the phrase.":
        "Zwei Eingangsgrößen, der Reihe nach geprüft. Die erste passende "
        "Sprosse gewinnt, die Leiter liest sich also von oben nach unten. "
        "Beide Eingangsgrößen sind Mediane über {minutes} Minuten und nicht "
        "der jüngste Messwert — die Sprossen liegen so dicht beieinander, "
        "dass ein einzelner Ausreißer den Text sonst umkippen ließe.",
    "When": "Wann",
    "It says": "Sagt",
    "Rain followed": "Regen folgte",
    "Any hour at all, for comparison": "Beliebige Stunde, zum Vergleich",
    "about twice the base rate, whatever the barometer says":
        "etwa doppelt so oft wie im Mittel, was auch immer das Barometer sagt",
    "Right now the pressure ranks at {pct}% of its last {days} days.":
        "Derzeit liegt der Luftdruck auf Rang {pct} % seiner letzten {days} Tage.",
    "Higher than {pct}% of the last {days} days":
        "Höher als {pct} % der letzten {days} Tage",
    "The reading itself is whatever the sensor reports. This app never "
    "reduces it to sea level and cannot tell whether the sensor already "
    "has: across the archive it averages {avg} hPa, where sea-level "
    "pressure averages about 1013. So read it as this balcony's own "
    "barometer rather than as the figure a forecast quotes — and note "
    "that nothing here reads it as an absolute, which is exactly what the "
    "percentile is for.":
        "Der Messwert ist das, was der Sensor meldet. Diese App rechnet ihn "
        "nie auf Meereshöhe um und kann nicht erkennen, ob der Sensor das "
        "bereits tut: über das gesamte Archiv liegt er im Mittel bei {avg} "
        "hPa, während der Luftdruck auf Meereshöhe im Mittel etwa 1013 "
        "beträgt. Lies ihn also als das Barometer dieses Balkons und nicht "
        "als den Wert, den eine Wettervorhersage nennt — und beachte, dass "
        "hier ohnehin nichts den absoluten Wert liest, wofür genau das "
        "Perzentil da ist.",
    "The rightmost column is how often measurable rain actually followed "
    "within {hours} hours, over the {days} days these rungs were fitted on. "
    "It is the whole claim being made: a phrase is worth showing only if the "
    "weather behind it differed from any other hour.":
        "Die rechte Spalte zeigt, wie oft tatsächlich messbarer Regen "
        "innerhalb von {hours} Stunden folgte, über die {days} Tage, auf die "
        "diese Sprossen angepasst wurden. Mehr wird nicht behauptet: Ein Text "
        "lohnt sich nur, wenn sich das Wetter dahinter von einer beliebigen "
        "anderen Stunde unterschied.",

    # The tier conditions (app/weather.py RULE_LADDER)
    "Pressure in the lowest {pct}% of 30 days, humidity above {rh}%":
        "Luftdruck in den untersten {pct} % von 30 Tagen, Feuchte über {rh} %",
    "Above {temp}°C and humidity above {rh}%, {start}:00 to {end}:59, "
    "{first} to {last}":
        "Über {temp} °C und Feuchte über {rh} %, {start}:00 bis {end}:59, "
        "{first} bis {last}",
    "Pressure in the lowest {pct}% of 30 days, drier than that":
        "Luftdruck in den untersten {pct} % von 30 Tagen, trockener als das",
    "Pressure in the middle of its range, {low}% to {high}%":
        "Luftdruck in der Mitte seiner Spanne, {low} % bis {high} %",
    "Pressure above the {pct}th percentile, humidity below {rh}%":
        "Luftdruck über dem {pct}. Perzentil, Feuchte unter {rh} %",

    # The tier conditions and notes of the composed ladder
    # (app/weather.py learned_ladder)
    "Rain model above {pct}%": "Regenmodell über {pct} %",
    "Sky model above {pct}%": "Wolkenmodell über {pct} %",
    "Sky model between {low}% and {high}%":
        "Wolkenmodell zwischen {low} % und {high} %",
    "Sky model below {pct}%, humidity below {rh}%":
        "Wolkenmodell unter {pct} %, Feuchte unter {rh} %",
    "Above {temp}°C and humidity above {rh}%, {start}:00 to {end}:59":
        "Über {temp} °C und Feuchte über {rh} %, {start}:00 bis {end}:59",
    "Dew-point spread under {spread}°C and humidity rising":
        "Taupunktdifferenz unter {spread} °C und steigende Feuchte",
    "fitted against observed rainfall":
        "an beobachtetem Niederschlag angepasst",
    "fitted against observed cloud cover":
        "an beobachteter Bewölkung angepasst",
    "Thunder model above {pct}%": "Gewittermodell über {pct} %",
    "Fog model above {pct}%": "Nebelmodell über {pct} %",
    "fitted against thunder the stations' observers reported":
        "an Gewittern angepasst, die die Beobachter der Stationen meldeten",
    "fitted against observed visibility":
        "an beobachteter Sichtweite angepasst",
    "hand-made: no thunder model has cleared its gates yet":
        "von Hand: noch kein Gewittermodell hat seine Hürden genommen",
    "hand-made: no fog model has cleared its gates yet":
        "von Hand: noch kein Nebelmodell hat seine Hürden genommen",

    # How the sky probability is worded (app/nowcast.py describe_sky)
    "mostly cloudy": "überwiegend bewölkt",
    "some cloud": "teils bewölkt",
    "mostly clear": "überwiegend klar",

    # The composed ladder's own section of the explainer
    "Evidence": "Grundlage",

    "Why the barometer is read as a level, not a tendency":
        "Warum das Barometer als Stand und nicht als Tendenz gelesen wird",
    '"Falling barometer means rain" is the first rule anyone reaches for, and '
    'on this station it is worse than useless. A fall of more than 1 hPa over '
    'six hours scored a Hanssen-Kuipers score of {kss} against rain within '
    'six hours; inside the wettest conditions, rain followed a rising '
    'barometer {rising}% of the time against {falling}% for a falling one — '
    'the wrong way round. The rules this ladder replaced were built entirely '
    'on tendency and scored {old}.':
        "„Fallendes Barometer heißt Regen“ ist die erste Regel, zu der jeder "
        "greift, und an dieser Station ist sie schlechter als nutzlos. Ein "
        "Fall von mehr als 1 hPa in sechs Stunden erreichte gegenüber Regen "
        "binnen sechs Stunden einen Hanssen-Kuipers-Wert von {kss}; unter den "
        "feuchtesten Bedingungen folgte Regen auf ein steigendes Barometer in "
        "{rising} % der Fälle gegenüber {falling} % bei fallendem — genau "
        "andersherum. Die Regeln, die diese Leiter ersetzt hat, beruhten "
        "vollständig auf der Tendenz und erreichten {old}.",
    "Where the pressure sits does carry signal, but no fixed hPa threshold "
    "survives the change of season: a plain 'below 1020 hPa' scored a "
    "critical success index of {low} in one month of the sample and {high} in "
    "another. Ranking the reading against the station's own last {days} days "
    "is stable across all of them, which is why the column above is a "
    "percentile. The tendency is still measured and shown on the pressure "
    "card, and the rain model reads it too — not as a rule, but as one "
    "signal among the others, where it does earn its place (see what each "
    "signal is worth, below).":
        "Wo der Luftdruck steht, trägt sehr wohl Information, aber keine "
        "feste hPa-Schwelle übersteht den Jahreszeitenwechsel: Ein schlichtes "
        "„unter 1020 hPa“ erreichte in einem Monat der Stichprobe einen "
        "Critical Success Index von {low} und in einem anderen {high}. Den "
        "Messwert gegen die eigenen letzten {days} Tage der Station zu "
        "sortieren, ist über alle hinweg stabil — deshalb steht oben ein "
        "Perzentil. Die Tendenz wird weiterhin gemessen und auf der "
        "Luftdruck-Kachel gezeigt, und auch das Regenmodell liest sie — "
        "nicht als Regel, sondern als eine Größe unter den anderen, wo sie "
        "sich durchaus bewährt (siehe unten, was jede Größe wert ist).",
    "Taken as a yes/no rain forecast the ladder scores CSI {csi} and KSS "
    "{kss}, against {old} for what it replaced.":
        "Als Ja/Nein-Regenvorhersage gelesen erreicht die Leiter CSI {csi} "
        "und KSS {kss}, gegenüber {old} für das, was sie ersetzt hat.",

    # The model, in full
    "🤖 The rain chance: a fitted model":
        "🤖 Die Regenwahrscheinlichkeit: ein angepasstes Modell",
    "Gradient-boosted trees: {trees} small decision trees, each splitting the "
    "hour on one signal at a time, whose outputs add up to log-odds that a "
    "sigmoid turns into a probability. Trees rather than a weighted sum, "
    "because these signals matter in combination — a sharp chill at "
    "saturation means rain under any barometer, and a sum can only add the "
    "two. Serving it is a walk down each tree, so the container carries no "
    "numpy and no scikit-learn; the training job in CI does, and it ships the "
    "trees back as plain arrays in JSON.":
        "Gradient-Boosting-Bäume: {trees} kleine Entscheidungsbäume, von "
        "denen jeder die Stunde Schritt für Schritt an jeweils einer Größe "
        "aufteilt; ihre Ausgaben summieren sich zu Log-Odds, die eine "
        "Sigmoidfunktion in eine Wahrscheinlichkeit verwandelt. Bäume statt "
        "einer gewichteten Summe, weil diese Größen im Zusammenspiel zählen — "
        "eine scharfe Abkühlung bei Sättigung bedeutet Regen, was auch immer "
        "das Barometer sagt, und eine Summe kann beides nur addieren. Das "
        "Auswerten ist ein Gang durch jeden Baum, der Container trägt also "
        "weder numpy noch scikit-learn; das tut der Trainingslauf in der CI, "
        "und zurück kommen die Bäume als schlichte Arrays in JSON.",
    "It was not fitted to this balcony. One summer of readings is all the "
    "archive holds, and a model that has only seen July learns that cool air "
    "means dry air — the model this replaced did, and with rain on the "
    "sensor in October it said 11%. So it learns from the weather service's "
    "stations, every season of many years, and this balcony is where every "
    "retrain has to prove itself.":
        "Angepasst wurde es nicht an diesen Balkon. Das Archiv umfasst einen "
        "einzigen Sommer, und ein Modell, das nur den Juli kennt, lernt, dass "
        "kühle Luft trockene Luft ist — das Vorgängermodell tat genau das und "
        "sagte im Oktober bei Regen auf dem Sensor 11 %. Deshalb lernt es von "
        "den Stationen des Wetterdienstes, aus jeder Jahreszeit vieler Jahre, "
        "und an diesem Balkon muss sich jedes neue Training bewähren.",
    "Question": "Frage",
    "At least {mm} mm of rain within {hours} hours, here":
        "Mindestens {mm} mm Regen innerhalb von {hours} Stunden, hier",
    "Learned from": "Gelernt von",
    "{n} weather-service stations": "{n} Stationen des Wetterdienstes",
    "{from_date} to {to_date}": "{from_date} bis {to_date}",
    "Fitted": "Angepasst",
    "data through {date}": "Daten bis {date}",
    "Sample": "Stichprobe",
    "{n} hours": "{n} Stunden",
    "{pct}% of them wet": "davon {pct} % nass",
    "Fires above": "Schlägt an ab",

    "How well it scores": "Wie gut es abschneidet",
    "Mean squared error of the probability. Lower is better.":
        "Mittlerer quadratischer Fehler der Wahrscheinlichkeit. Kleiner ist besser.",
    "How often it ranks a wet hour above a dry one. 0.5 is a coin flip.":
        "Wie oft es eine nasse Stunde über eine trockene stellt. 0,5 ist ein "
        "Münzwurf.",
    "Critical success index of the yes/no call. Higher is better.":
        "Critical Success Index der Ja/Nein-Entscheidung. Größer ist besser.",
    "Hanssen-Kuipers score: hit rate minus false-alarm rate.":
        "Hanssen-Kuipers-Wert: Trefferrate minus Fehlalarmrate.",
    "This model": "Dieses Modell",
    "The ladder above": "Die Leiter oben",
    "Always saying {pct}%": "Immer {pct} % sagen",
    "On the most recent year at those stations, {from_date} to {to_date}: "
    "{hours} hours that the fit being scored never saw, every season once.":
        "Auf dem jüngsten Jahr an diesen Stationen, {from_date} bis "
        "{to_date}: {hours} Stunden, die die bewertete Anpassung nie gesehen "
        "hat, jede Jahreszeit einmal.",
    "Its skill over the base rate, season by season: winter {winter}, spring "
    "{spring}, summer {summer}, autumn {autumn}. A model fitted to one summer "
    "has no business in any of the other three — this one has seen each of "
    "them many times.":
        "Seine Güte gegenüber der Grundrate, Jahreszeit für Jahreszeit: "
        "Winter {winter}, Frühling {spring}, Sommer {summer}, Herbst "
        "{autumn}. Ein Modell, das an einen einzigen Sommer angepasst ist, "
        "hat in den anderen dreien nichts verloren — dieses hat jede davon "
        "viele Male gesehen.",
    "On this balcony's own readings, {from_date} to {to_date} — {hours} "
    "hours, {wet}% of them followed by rain at the nearest weather-service "
    "gauge — it scored Brier {brier}, a skill of {bss}, AUC {auc}, where the "
    "ladder ranked the same hours at AUC {rules}. That is the test a "
    "weather-service screen cannot pass on this sensor's behalf.":
        "Auf den eigenen Messwerten dieses Balkons, {from_date} bis "
        "{to_date} — {hours} Stunden, auf {wet} % davon folgte Regen am "
        "nächstgelegenen Regenmesser des Wetterdienstes — erreichte es "
        "Brier {brier}, eine Güte von {bss} und AUC {auc}; die Leiter "
        "sortierte dieselben Stunden mit AUC {rules}. Diese Prüfung kann "
        "eine Wetterhütte des Wetterdienstes diesem Sensor nicht abnehmen.",
    "Straight from the weather-service stations it scored a skill of "
    "{straight} on those hours: it ranked them well, but its probabilities did "
    "not fit this sensor, which is sun-baked and holds dew where a ventilated "
    "screen does neither. So the last step is a calibration fitted to the "
    "balcony's own record — two numbers that rescale the log-odds without "
    "changing which hour ranks above which — and the figures above are for the "
    "calibrated model, each stretch of the record scored by a calibration that "
    "was fitted without it.":
        "Direkt von den Stationen des Wetterdienstes übernommen erreichte es "
        "auf diesen Stunden eine Güte von {straight}: Es sortierte sie gut, "
        "aber seine Wahrscheinlichkeiten passten nicht zu diesem Sensor, der "
        "in der Sonne aufheizt und Tau hält, wo eine belüftete Wetterhütte "
        "beides nicht tut. Der letzte Schritt ist daher eine Kalibrierung an "
        "den eigenen Messwerten des Balkons — zwei Zahlen, die die Log-Odds "
        "umskalieren, ohne zu ändern, welche Stunde vor welcher liegt —, und "
        "die Werte oben gelten für das kalibrierte Modell, wobei jeder "
        "Abschnitt der Messreihe mit einer Kalibrierung bewertet wurde, die "
        "ohne ihn angepasst wurde.",
    "A retrained model only ships if it clears the same gates on both tests: "
    "skill over the base rate, ranking hours at least as well as the ladder, "
    "and no sharp regression against the model already deployed. Refusing "
    "to ship is a normal outcome.":
        "Ein neu trainiertes Modell geht nur live, wenn es in beiden "
        "Prüfungen dieselben Hürden nimmt: Güte gegenüber der Grundrate, "
        "mindestens so gute Sortierung der Stunden wie die Leiter und kein "
        "deutlicher Rückschritt gegenüber dem bereits ausgelieferten Modell. "
        "Nicht auszuliefern ist ein normaler Ausgang.",
    # ── The live verification ──────────────────────────────────────────────
    "And has it been right?": "Und hatte es recht?",
    "Every hour, what the page was showing is written down. Once the weather "
    "has happened those are scored against the nearest weather-service rain "
    "gauge — the same kind of observation the model learned from. This is "
    "the deployed model's own record — not a fit to a held-out past, but the "
    "thing you were actually shown.":
        "Stündlich wird festgehalten, was die Seite gerade anzeigte. Sobald "
        "das Wetter eingetreten ist, wird das am nächstgelegenen Regenmesser "
        "des Wetterdienstes bewertet — dieselbe Art Beobachtung, aus der das "
        "Modell gelernt hat. Das ist die "
        "Bilanz des ausgelieferten Modells — keine Anpassung an eine "
        "zurückgehaltene Vergangenheit, sondern das, was tatsächlich zu "
        "sehen war.",
    "{hours} hours logged, {from_date} to {to_date}, of which {wet}% saw "
    "rain. A calibrated probability matches its own claim: in the rows where "
    "it said 40%, it should have rained about 40% of the time.":
        "{hours} Stunden protokolliert, {from_date} bis {to_date}, davon "
        "{wet} % mit Regen. Eine kalibrierte Wahrscheinlichkeit hält, was "
        "sie sagt: In den Zeilen, in denen 40 % stand, sollte es in etwa "
        "40 % der Fälle geregnet haben.",
    "When it said": "Angesagt",
    "It rained": "Geregnet",
    "Hours": "Stunden",
    "Over those hours the deployed model scored Brier {brier}, a skill of "
    "{bss} over climatology, AUC {auc}. The threshold ladder, scored on "
    "exactly the same hours, managed CSI {rules_csi} against the model's "
    "{model_csi}.":
        "Über diese Stunden erreichte das ausgelieferte Modell Brier "
        "{brier}, eine Güte von {bss} gegenüber der Klimatologie, AUC "
        "{auc}. Die Schwellenwert-Leiter kam auf denselben Stunden auf CSI "
        "{rules_csi}, das Modell auf {model_csi}.",
    "Those hours span {n} retrained models, because the model is refitted "
    "weekly. That is why this is logged rather than replayed: replaying the "
    "archive would credit every past hour to the model shipped today.":
        "Diese Stunden umfassen {n} neu trainierte Modelle, denn das Modell "
        "wird wöchentlich neu angepasst. Deshalb wird protokolliert statt "
        "nachgerechnet: Ein Nachspielen des Archivs würde jede vergangene "
        "Stunde dem heute ausgelieferten Modell zuschreiben.",

    "What each signal is worth": "Was jede Größe wert ist",
    "Why the percentage means rain here":
        "Warum der Prozentwert Regen hier meint",
    "The labels are rain gauges — each one standing beside the sensors the "
    "model learned from — so the question is rain at a point like this "
    "balcony, not somewhere in the district. They replaced labels from a "
    "{km} km reanalysis, and that was measured rather than assumed: scored "
    "on the same two years at eight stations, the same trees ranked wet "
    "hours above dry ones as well against the gauges (AUC {auc_gauge}) as "
    "against the reanalysis (AUC {auc_area}). Point rain had looked "
    "unpredictable only because it was being asked of one summer and a "
    "weighted sum.":
        "Die Zielwerte sind Regenmesser — jeder steht neben den Sensoren, "
        "von denen das Modell gelernt hat —, die Frage lautet also Regen an "
        "einem Punkt wie diesem Balkon, nicht irgendwo im Landkreis. Sie "
        "ersetzen Zielwerte aus einer Reanalyse mit {km} km Auflösung, und "
        "das wurde nachgemessen statt angenommen: Auf denselben zwei Jahren "
        "an acht Stationen bewertet, sortierten dieselben Bäume nasse "
        "Stunden gegen die Regenmesser (AUC {auc_gauge}) genauso gut vor "
        "trockene wie gegen die Reanalyse (AUC {auc_area}). Punktregen wirkte "
        "nur deshalb unvorhersagbar, weil man ihn einem einzigen Sommer und "
        "einer gewichteten Summe abverlangte.",
    "The same fit and the same held-out year, refitted from scratch without "
    "each signal in turn. Positive means the model got worse without it — "
    "that is what having evidence for a signal looks like. A row near zero "
    "is a signal the model could lose without noticing, usually because "
    "another one already carries what it knows.":
        "Dieselbe Anpassung und dasselbe zurückgehaltene Jahr, jeweils ohne "
        "eine Größe von Grund auf neu gefittet. Positiv heißt: ohne sie "
        "wurde das Modell schlechter — so sieht Evidenz für eine Größe aus. "
        "Eine Zeile nahe null ist eine Größe, deren Verlust das Modell nicht "
        "bemerken würde, meist weil eine andere bereits trägt, was sie "
        "weiß.",
    "Without": "Ohne",
    "Cost of dropping it": "Kosten des Weglassens",
    "How much worse the Brier score gets without this signal.":
        "Um wie viel schlechter der Brier-Score ohne diese Größe wird.",
    "What is driving the number right now":
        "Was die Zahl gerade antreibt",
    "Each tree walks one path from its root to a leaf, and every step down "
    "it is a split on one signal that moves the tree's output up or down. "
    "Credit each step to the signal that made it, add them up over every "
    "tree, and you have the effects below: the starting point plus all of "
    "them is exactly the number the sigmoid squashes, which makes this table "
    "the prediction rather than a picture of it. There is no fixed weight to "
    "print beside them — what a signal is worth depends on the others, which "
    "is the reason for using trees.":
        "Jeder Baum geht einen Pfad von der Wurzel bis zu einem Blatt, und "
        "jeder Schritt darauf ist eine Teilung an einer Größe, die die "
        "Ausgabe des Baums nach oben oder unten bewegt. Schreibt man jeden "
        "Schritt der Größe zu, die ihn ausgelöst hat, und summiert über alle "
        "Bäume, ergeben sich die Effekte unten: Der Ausgangswert plus alle "
        "Effekte ist genau die Zahl, die die Sigmoidfunktion zusammenstaucht "
        "— diese Tabelle ist also die Vorhersage und nicht ein Bild davon. "
        "Ein festes Gewicht lässt sich daneben nicht angeben — was eine "
        "Größe wert ist, hängt von den anderen ab, und genau deshalb sind es "
        "Bäume.",
    "Signal": "Größe",
    "Now": "Jetzt",
    "Effect now": "Effekt jetzt",
    "The log-odds this signal moved the prediction by, right now.":
        "Um so viele Log-Odds hat diese Größe die Vorhersage gerade bewegt.",
    "Starting point, before any signal": "Ausgangswert, vor allen Größen",
    "Total, squashed to a probability":
        "Summe, zur Wahrscheinlichkeit gestaucht",

    # Feature names (app/nowcast.py FEATURE_FORMATS)
    "Peak humidity, 1 h": "Höchste Feuchte, 1 Std.",
    "Peak humidity, 3 h": "Höchste Feuchte, 3 Std.",
    "Time saturated, 3 h": "Zeit in Sättigung, 3 Std.",
    "Dew-point spread": "Taupunktdifferenz",
    "Temperature change, 1 h": "Temperaturänderung, 1 Std.",
    "Temperature change, 3 h": "Temperaturänderung, 3 Std.",
    "Temperature change, 24 h": "Temperaturänderung, 24 Std.",
    "Dew-point change, 3 h": "Taupunktänderung, 3 Std.",
    "Humidity change, 3 h": "Feuchteänderung, 3 Std.",
    "Pressure rank, 30 days": "Luftdruck-Rang, 30 Tage",
    "Pressure rank, 7 days": "Luftdruck-Rang, 7 Tage",
    "Pressure change, 1 h": "Luftdruckänderung, 1 Std.",
    "Pressure change, 3 h": "Luftdruckänderung, 3 Std.",
    "Pressure change, 6 h": "Luftdruckänderung, 6 Std.",
    "Pressure change, 12 h": "Luftdruckänderung, 12 Std.",
    "Temperature swing, 3 h": "Temperaturspanne, 3 Std.",
    "Temperature swing, 6 h": "Temperaturspanne, 6 Std.",
    "Temperature swing, 12 h": "Temperaturspanne, 12 Std.",
    "Temperature swing, 24 h": "Temperaturspanne, 24 Std.",
    "Humidity swing, 6 h": "Feuchtespanne, 6 Std.",
    "Lowest humidity, 24 h": "Niedrigste Feuchte, 24 Std.",
    "Hour of the day": "Uhrzeit",

    # ── Current conditions ─────────────────────────────────────────────────
    "Temperature": "Temperatur",
    "Humidity": "Luftfeuchtigkeit",
    "Pressure": "Luftdruck",
    "Feels like {value}°C": "Gefühlt {value} °C",
    "Dew point": "Taupunkt",
    "Dew point {value}°C": "Taupunkt {value} °C",
    "vs {hours}h ago: {delta}°C": "vs. vor {hours} h: {delta} °C",
    "vs {hours}h ago: {delta}%": "vs. vor {hours} h: {delta} %",

    # ── Charts ─────────────────────────────────────────────────────────────
    "Chart period": "Diagrammzeitraum",
    "24 Hours": "24 Stunden",
    "7 Days": "7 Tage",
    "30 Days": "30 Tage",
    "All Time": "Gesamt",
    "Min": "Min",
    "Max": "Max",
    "Average": "Mittel",
    "All years, coldest to warmest": "Alle Jahre, kälteste bis wärmste",
    "The charting library did not load, so the graphs are missing.":
        "Die Diagrammbibliothek wurde nicht geladen, daher fehlen die Grafiken.",

    # Read aloud in place of the chart, never shown. A screen reader finds a
    # bare <canvas> and says nothing at all, so these carry what a summary
    # can carry: which measurement, over what, and how far it ranged.
    "{metric} over {period}: {min} to {max} {unit}":
        "{metric} über {period}: {min} bis {max} {unit}",
    "{metric}: {min} to {max} {unit}": "{metric}: {min} bis {max} {unit}",
    "{metric} chart, no readings": "{metric}-Diagramm, keine Messwerte",
    "Monthly average, minimum and maximum temperature for {year}: {min} to {max} °C":
        "Monatliche Durchschnitts-, Tiefst- und Höchsttemperatur für {year}: "
        "{min} bis {max} °C",
    "Monthly temperatures for {year}, no readings":
        "Monatstemperaturen für {year}, keine Messwerte",
    "{n} interval left out for having too few readings to average":
        "{n} Intervall ausgelassen: zu wenige Messwerte zum Mitteln",
    "{n} intervals left out for having too few readings to average":
        "{n} Intervalle ausgelassen: zu wenige Messwerte zum Mitteln",
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
    "vs the last {days} days": "vs. die letzten {days} Tage",
    "warmer than {n} of {days}": "wärmer als {n} von {days}",
    "Average temperature": "Durchschnittstemperatur",
    "Average humidity": "Durchschnittliche Luftfeuchtigkeit",
    "Readings": "Messwerte",
    "at {time}": "um {time}",

    # ── Climate ────────────────────────────────────────────────────────────
    "Climate": "Klima",
    "{year} average": "Mittel {year}",
    "min {min} max {max}": "min {min} max {max}",

    # ── Calendar ───────────────────────────────────────────────────────────
    "Previous year": "Vorheriges Jahr",
    "Next year": "Nächstes Jahr",
    "Yearly averages, one year per page":
        "Jahresmittel, ein Jahr pro Seite",
    "Temperature Calendar": "Temperaturkalender",
    "Previous month": "Vorheriger Monat",
    "Next month": "Nächster Monat",
    "Temperature calendar, one month per page":
        "Temperaturkalender, ein Monat pro Seite",
    "The calendar pages back {n} months; the archive starts earlier. The "
    "records and the climate card still read all of it.":
        "Der Kalender reicht {n} Monate zurück; das Archiv beginnt früher. "
        "Die Rekorde und die Klimakarte lesen weiterhin alles davon.",
    "Cold": "Kalt",
    "Hot": "Heiß",
    "Avg": "Ø",
    "Low": "Tief",
    "High": "Hoch",
    "No data": "Keine Daten",
    "{count} day with data": "{count} Tag mit Daten",
    "{count} days with data": "{count} Tage mit Daten",

    # ── How long ago the last reading arrived ──────────────────────────────
    "just now": "gerade eben",
    "{n} minute ago": "vor {n} Minute",
    "{n} minutes ago": "vor {n} Minuten",
    "{n} hour ago": "vor {n} Stunde",
    "{n} hours ago": "vor {n} Stunden",
    "Last updated {ago}": "Zuletzt aktualisiert {ago}",
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


#: How long an explicit choice is remembered for, in seconds.
LANGUAGE_COOKIE = "lang"
LANGUAGE_COOKIE_MAX_AGE = 365 * 24 * 60 * 60


def negotiate(
    header: str | None,
    override: str | None = None,
    remembered: str | None = None,
) -> str:
    """Pick a language: what was asked for, then what was chosen, then the browser.

    ``override`` is ``?lang=`` on the URL and wins outright. ``remembered``
    is the cookie that same parameter set on a previous visit, and beats the
    header — a reader who has said "English" has said it, and saying it again
    every visit is not a preference, it is a chore.

    The header comes last because it is a guess about a person made from an
    OS setting. It is a good guess and usually right, but on Windows
    ``Accept-Language`` is built from the *preferred languages* list, which
    the region seeds: set the region to Germany with an English display
    language and the browser asks for German. That is the case this
    precedence exists for.

    Only the primary subtag matters — ``de-AT`` and ``de-CH`` are German here.
    Quality values are honoured, so a browser configured for French first and
    German second gets German rather than the English fallback.
    """
    explicit = chosen_language(override) or chosen_language(remembered)
    if explicit:
        return explicit
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


def chosen_language(value: str | None) -> str | None:
    """The language ``value`` names, or ``None`` if this page does not speak it.

    What tells "the reader asked for German" apart from "the reader typed
    something": only the first is worth remembering, so the caller that sets
    the cookie asks this rather than asking what the page ended up rendering.
    """
    if not value:
        return None
    tag = _primary(value)
    return tag if tag in LANGUAGES else None


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


def rule_describer(lang: str) -> Callable[..., str]:
    """A ``rule(tier)`` that writes one forecast tier's condition, for Jinja.

    :mod:`app.weather` holds the tiers as a message id and raw numbers, and
    knows nothing about languages. This is where those numbers pick up the
    right separators and the month indices become month names — the same
    split the rest of the page uses, with the catalogue at the edge.
    """
    t = translator(lang)
    months = MONTHS_SHORT[lang]

    def rule(tier: object) -> str:
        fields: dict[str, object] = {}
        for key, value in tier.fields.items():  # type: ignore[attr-defined]
            if key in ("month_first", "month_last"):
                fields[key.removeprefix("month_")] = months[int(value) - 1]
            else:
                # Thresholds are carried as floats; none of them is meant to
                # be read with a decimal place unless it actually has one.
                digits = 0 if float(value).is_integer() else 1
                fields[key] = format_number(value, digits, lang)
        return t(tier.condition, **fields)  # type: ignore[attr-defined]

    return rule


def date_formatter(lang: str) -> Callable[[str], str]:
    """A ``date(timestamp)`` bound to one language, for the template context."""

    def date(timestamp: str) -> str:
        return format_date(timestamp, lang)

    return date


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


#: How each language writes a date. ``{d}`` is the day without a leading
#: zero and ``{dd}`` with one — English writes "1 Sep", German "01.09." —
#: ``{m}`` the zero-padded month, ``{mon}`` the short month name, ``{y}``
#: the year.
DATE_FORMATS = {"en": "{d} {mon} {y}", "de": "{dd}.{m}.{y}"}

#: Date and clock together. The clock stays 24-hour in both languages, which
#: is what this page has always shown.
DATETIME_FORMATS = {"en": "{date}, {time}", "de": "{date}, {time}"}


def format_date(timestamp: str, lang: str = DEFAULT_LANGUAGE) -> str:
    """A stored timestamp's date, written the way the language writes it.

    Takes the stored ``YYYY-MM-DD HH:MM:SS`` (or just its date half) and
    reformats the string — no parsing into a datetime, because these are
    naive local wall clocks and turning them into instants is exactly the
    mistake the rest of the codebase avoids.

    Done by hand for the same reason :func:`format_number` is: ``locale`` is
    process-global and would make the answer depend on what the container
    happens to have installed.
    """
    if not timestamp or len(timestamp) < 10:
        return timestamp or ""
    year, month, day = timestamp[:4], timestamp[5:7], timestamp[8:10]
    months = MONTHS_SHORT.get(lang, MONTHS_SHORT[DEFAULT_LANGUAGE])
    try:
        short = months[int(month) - 1]
    except (ValueError, IndexError):
        return timestamp
    template = DATE_FORMATS.get(lang, DATE_FORMATS[DEFAULT_LANGUAGE])
    return template.format(
        d=day.lstrip("0") or "0", dd=day, m=month, mon=short, y=year
    )


def format_datetime(timestamp: str, lang: str = DEFAULT_LANGUAGE) -> str:
    """A stored timestamp as a date and a 24-hour clock."""
    if len(timestamp) < 16:
        return format_date(timestamp, lang)
    template = DATETIME_FORMATS.get(lang, DATETIME_FORMATS[DEFAULT_LANGUAGE])
    return template.format(date=format_date(timestamp, lang), time=timestamp[11:16])


#: Thresholds for :func:`format_relative`, coarsest last. Each is the number
#: of seconds a unit holds, the singular message id and the plural one.
_RELATIVE_UNITS = (
    (60, "{n} minute ago", "{n} minutes ago"),
    (3600, "{n} hour ago", "{n} hours ago"),
)

#: Under a minute old is "just now" — a reading that arrived seconds ago is
#: not usefully described as "0 minutes ago".
RELATIVE_JUST_NOW_SECONDS = 60

#: Past a day, a relative age stops being informative and the date is what
#: the reader actually wants.
RELATIVE_MAX_SECONDS = 86_400


def format_relative(seconds: float, lang: str = DEFAULT_LANGUAGE) -> str | None:
    """How long ago, in words, or ``None`` when a date would serve better.

    ``None`` means "too old to phrase this way" — the caller then shows the
    timestamp itself. A negative age (a clock a little ahead, or a reading
    posted with a future timestamp) reads as just now rather than as a
    nonsense like "-1 minutes ago".
    """
    t = translator(lang)
    if seconds < RELATIVE_JUST_NOW_SECONDS:
        return t("just now")
    if seconds >= RELATIVE_MAX_SECONDS:
        return None

    for size, singular, plural in reversed(_RELATIVE_UNITS):
        if seconds >= size:
            count = int(seconds // size)
            return t(singular if count == 1 else plural, n=count)
    return t("just now")


def page_payload(lang: str) -> dict:
    """What the browser needs to speak the same language as the render.

    The whole catalogue goes over, not a hand-picked subset: a list of "these
    strings are the JavaScript ones" is a list that goes stale silently. It is
    around 20 KB of German on a page that is already ``no-store`` — which is
    why the app compresses (see :func:`app.main.create_app`), that being the
    right answer to its size rather than pruning it.
    """
    return {
        "lang": lang,
        "locale": LOCALES.get(lang, LOCALES[DEFAULT_LANGUAGE]),
        "decimal": SEPARATORS.get(lang, SEPARATORS[DEFAULT_LANGUAGE])[0],
        "thousands": SEPARATORS.get(lang, SEPARATORS[DEFAULT_LANGUAGE])[1],
        "strings": CATALOGUES.get(lang, {}),
        # The date templates go over for the same reason the separators do:
        # the poller rewrites the timestamp the render produced, and a date
        # that changes shape after sixty seconds reads as a bug.
        "date_format": DATE_FORMATS.get(lang, DATE_FORMATS[DEFAULT_LANGUAGE]),
        "datetime_format": DATETIME_FORMATS.get(
            lang, DATETIME_FORMATS[DEFAULT_LANGUAGE]
        ),
        "months_long": list(MONTHS_LONG.get(lang, MONTHS_LONG[DEFAULT_LANGUAGE])),
        "months_short": list(MONTHS_SHORT.get(lang, MONTHS_SHORT[DEFAULT_LANGUAGE])),
        "days_short": list(DAYS_SHORT.get(lang, DAYS_SHORT[DEFAULT_LANGUAGE])),
    }
