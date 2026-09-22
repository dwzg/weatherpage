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
    "Cloudy": "Bewölkt",
    "Little change": "Wenig Veränderung",
    "Not enough data": "Nicht genug Daten",

    # ── Nowcast (app/nowcast.py describe()) ────────────────────────────────
    "likely": "wahrscheinlich",
    "possible": "möglich",
    "unlikely": "unwahrscheinlich",
    "not expected": "nicht zu erwarten",
    "Rain nearby": "Regen in der Nähe",
    "in {hours} h": "in {hours} Std.",
    # Suffix on each card's trend arrow: "+1.2 °C /3h".
    "/{hours}h": "/{hours} Std.",
    "Learned from this station's own history":
        "Aus der eigenen Messreihe dieser Station gelernt",

    # ── Alert banners ──────────────────────────────────────────────────────
    "❄️ Frost warning — protect your plants!":
        "❄️ Frostwarnung — Pflanzen schützen!",
    "⚠️ No new readings — the sensor feed may be down":
        "⚠️ Keine neuen Messwerte — der Sensor meldet sich möglicherweise nicht",
    "⚠️ Not reachable — these readings may be out of date":
        "⚠️ Nicht erreichbar — diese Messwerte sind möglicherweise veraltet",

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

    # ── The deep dive ──────────────────────────────────────────────────────
    "The technical details": "Die technischen Details",

    # The outlook, in full
    "☀️ The outlook: a ladder of thresholds":
        "☀️ Die Aussicht: eine Leiter aus Schwellenwerten",
    "☀️ The outlook: one model and a ladder":
        "☀️ Die Aussicht: ein Modell und eine Leiter",
    "Tested in order, and the first rung that matches wins. The rain rungs "
    "read the fitted probability shown on the pill above — the same number, "
    "so the two cannot disagree. Below them the ladder falls back to where "
    "the pressure sits and how humid the air is, because no cloud model has "
    "cleared its gates yet; when one does, those rungs become measurements "
    "too.":
        "Der Reihe nach geprüft; die erste zutreffende Sprosse gewinnt. Die "
        "Regensprossen lesen die angepasste Wahrscheinlichkeit, die oben auf "
        "der Plakette steht — dieselbe Zahl, die beiden können sich also "
        "nicht widersprechen. Darunter greift die Leiter auf den Stand des "
        "Luftdrucks und die Luftfeuchtigkeit zurück, denn noch hat kein "
        "Wolkenmodell seine Hürden genommen; sobald eines das schafft, "
        "werden auch diese Sprossen zu Messungen.",
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
    "hand-made: no label source scores thunderstorms here":
        "von Hand: keine Datenquelle bewertet hier Gewitter",
    "hand-made: the archive records no fog at all":
        "von Hand: das Archiv verzeichnet überhaupt keinen Nebel",

    # How the sky probability is worded (app/nowcast.py describe_sky)
    "mostly cloudy": "überwiegend bewölkt",
    "some cloud": "teils bewölkt",
    "mostly clear": "überwiegend klar",

    # The composed ladder's own section of the explainer
    "☀️ The outlook: two models and two rules":
        "☀️ Die Aussicht: zwei Modelle und zwei Regeln",
    "Evidence": "Grundlage",
    "Tested in order, and the first rung that matches wins. Four of these "
    "rungs read a fitted probability — one model for rain, one for cloud, "
    "both from the same ten measurements. Two of them do not, and cannot: "
    "see below.":
        "Der Reihe nach geprüft; die erste zutreffende Sprosse gewinnt. Vier "
        "dieser Sprossen lesen eine angepasste Wahrscheinlichkeit — ein "
        "Modell für Regen, eines für Bewölkung, beide aus denselben zehn "
        "Messwerten. Zwei tun das nicht, und können es nicht: siehe unten.",
    "Why two of them are still hand-made: no label source scores either. Over "
    "two years of the reanalysis at this location there are zero fog codes "
    "and zero thunderstorm codes, and its convective-energy field is empty. A "
    "model cannot be fitted to a label that does not exist, so those rungs "
    "stay thresholds and say so.":
        "Warum zwei davon weiterhin von Hand stammen: Keine Datenquelle "
        "bewertet sie. Über zwei Jahre der Reanalyse an diesem Ort gibt es "
        "null Nebel-Codes und null Gewitter-Codes, und ihr Feld für "
        "konvektive Energie ist leer. Ein Modell lässt sich nicht an ein "
        "Label anpassen, das es nicht gibt; diese Sprossen bleiben daher "
        "Schwellenwerte und sagen das auch.",
    "The cloud model was fitted on {samples} hours and scored walk-forward: "
    "AUC {auc}, Brier skill {bss} against climatology. What it replaces is "
    "the humidity threshold this ladder used to read, which scored AUC "
    "{rules}.":
        "Das Wolkenmodell wurde an {samples} Stunden angepasst und "
        "vorwärtsschreitend bewertet: AUC {auc}, Brier-Skill {bss} gegenüber "
        "der Klimatologie. Es ersetzt den Feuchte-Schwellenwert, den diese "
        "Leiter zuvor las und der AUC {rules} erreichte.",
    "Right now it puts the chance of a mostly overcast next {hours} hours "
    "at {pct}%.":
        "Aktuell beziffert es die Chance auf überwiegend bedeckte nächste "
        "{hours} Stunden mit {pct} %.",

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
    "card, because it is a fact about the last six hours — it is just not "
    "evidence about the next six.":
        "Wo der Luftdruck steht, trägt sehr wohl Information, aber keine "
        "feste hPa-Schwelle übersteht den Jahreszeitenwechsel: Ein schlichtes "
        "„unter 1020 hPa“ erreichte in einem Monat der Stichprobe einen "
        "Critical Success Index von {low} und in einem anderen {high}. Den "
        "Messwert gegen die eigenen letzten {days} Tage der Station zu "
        "sortieren, ist über alle hinweg stabil — deshalb steht oben ein "
        "Perzentil. Die Tendenz wird weiterhin gemessen und auf der "
        "Luftdruck-Kachel gezeigt, denn sie ist eine Tatsache über die "
        "vergangenen sechs Stunden — nur eben kein Beleg für die nächsten "
        "sechs.",
    "Taken as a yes/no rain forecast the ladder scores CSI {csi} and KSS "
    "{kss}, against {old} for what it replaced.":
        "Als Ja/Nein-Regenvorhersage gelesen erreicht die Leiter CSI {csi} "
        "und KSS {kss}, gegenüber {old} für das, was sie ersetzt hat.",

    # The model, in full
    "🤖 The rain chance: a fitted model":
        "🤖 Die Regenwahrscheinlichkeit: ein angepasstes Modell",
    "A logistic regression over {n} features. Each is standardised against "
    "its training mean, multiplied by a weight, and added up; a sigmoid turns "
    "that sum into a probability. Serving it is a dot product and a sigmoid, "
    "so the container carries no numpy and no scikit-learn — the training job "
    "in CI does, and all it ships back is a small JSON file of names, means, "
    "scales and weights.":
        "Eine logistische Regression über {n} Merkmale. Jedes wird gegen "
        "seinen Trainingsmittelwert standardisiert, mit einem Gewicht "
        "multipliziert und aufaddiert; eine Sigmoidfunktion macht aus dieser "
        "Summe eine Wahrscheinlichkeit. Das Auswerten ist ein Skalarprodukt "
        "und eine Sigmoidfunktion, der Container trägt also weder numpy noch "
        "scikit-learn — das tut der Trainingslauf in der CI, und zurück kommt "
        "nur eine kleine JSON-Datei mit Namen, Mittelwerten, Skalen und "
        "Gewichten.",
    "Question": "Frage",
    "{mm} mm of rain within {hours} hours, in the area":
        "{mm} mm Regen innerhalb von {hours} Stunden, in der Umgebung",
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
    # ── The live verification ──────────────────────────────────────────────
    "And has it been right?": "Und hatte es recht?",
    "Every hour, what the page was showing is written down. Once the weather "
    "has happened those are scored against the same observations the model "
    "was trained on. This is the deployed model's own record — not a fit to a "
    "held-out past, but the thing you were actually shown.":
        "Stündlich wird festgehalten, was die Seite gerade anzeigte. Sobald "
        "das Wetter eingetreten ist, wird das gegen dieselben Beobachtungen "
        "bewertet, mit denen das Modell trainiert wurde. Das ist die "
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

    "Scored walk-forward with weekly refits, never on hours it was fitted on. "
    "A retrained model only ships if it clears all three gates: skill over "
    "climatology, ranking at least as well as the ladder, and no sharp "
    "regression against the model already deployed. Refusing to ship is a "
    "normal outcome.":
        "Vorwärtsrollend bewertet, mit wöchentlicher Neuanpassung, nie auf "
        "Stunden, auf die es angepasst wurde. Ein neu trainiertes Modell geht "
        "nur live, wenn es alle drei Hürden nimmt: Güte gegenüber der "
        "Klimatologie, mindestens so gute Sortierung wie die Leiter und kein "
        "deutlicher Rückschritt gegenüber dem bereits ausgelieferten Modell. "
        "Nicht auszuliefern ist ein normaler Ausgang.",

    "Why the percentage means rain in the area":
        "Warum der Prozentwert Regen in der Umgebung meint",
    'The labels it learned from are a {km} km reanalysis — "did it rain '
    'around here", not "did it rain on this balcony". That is deliberate, and '
    'it was measured: trained and judged on a 2 km series instead, the same '
    'features manage AUC {auc} against {auc_good} on the reanalysis. Point '
    'rain is a few percent of hours and turns on convective detail a '
    'barometer cannot see; the synoptic question is the one these sensors can '
    'answer.':
        "Die Zielwerte, aus denen es gelernt hat, stammen aus einer "
        "Reanalyse mit {km} km Auflösung — „hat es hier in der Gegend "
        "geregnet“, nicht „hat es auf diesem Balkon geregnet“. Das ist "
        "Absicht, und es wurde nachgemessen: Auf einer 2-km-Reihe trainiert "
        "und bewertet schaffen dieselben Merkmale AUC {auc} gegenüber "
        "{auc_good} auf der Reanalyse. Punktregen macht nur wenige Prozent "
        "der Stunden aus und hängt an konvektiven Details, die ein Barometer "
        "nicht sieht; die großräumige Frage ist die, die diese Sensoren "
        "beantworten können.",
    "Held against point rain at the station, the same model scores Brier "
    "{brier} with a skill of {bss} — negative, because it is quoting area "
    "odds at a question about one roof. That is the honest cost of the "
    "choice, and it is why the pill says nearby.":
        "Gegen den Punktregen an der Station gehalten erreicht dasselbe "
        "Modell Brier {brier} bei einer Güte von {bss} — negativ, weil es auf "
        "eine Frage nach einem einzelnen Dach mit Chancen für die Umgebung "
        "antwortet. Das ist der ehrliche Preis dieser Entscheidung, und "
        "deshalb steht auf der Plakette „in der Nähe“.",

    "What is driving the number right now":
        "Was die Zahl gerade antreibt",
    "Every weight below is in log-odds, which is the unit the model actually "
    "adds in. The starting point plus all ten weights is the number the "
    "sigmoid squashes, so this table is the prediction rather than a picture "
    "of it.":
        "Jedes Gewicht unten steht in Log-Odds, der Einheit, in der das "
        "Modell tatsächlich addiert. Der Ausgangswert plus alle zehn Gewichte "
        "ergibt die Zahl, die die Sigmoidfunktion zusammenstaucht — diese "
        "Tabelle ist also die Vorhersage und nicht ein Bild davon.",
    "Signal": "Größe",
    "Now": "Jetzt",
    "vs normal": "vs. normal",
    "Standard deviations from the training mean.":
        "Standardabweichungen vom Trainingsmittelwert.",
    "Weight": "Gewicht",
    "Starting point, before any signal": "Ausgangswert, vor allen Größen",
    "Total, squashed to a probability":
        "Summe, zur Wahrscheinlichkeit gestaucht",

    # Feature names (app/nowcast.py FEATURE_FORMATS)
    "Pressure rank, 30 days": "Luftdruck-Rang, 30 Tage",
    "Pressure rank, 7 days": "Luftdruck-Rang, 7 Tage",
    "Peak humidity, 6 h": "Höchste Feuchte, 6 Std.",
    "Humidity change, 3 h": "Feuchteänderung, 3 Std.",
    "Humidity change, 6 h": "Feuchteänderung, 6 Std.",
    "Dew-point spread": "Taupunktdifferenz",
    "Pressure change, 6 h": "Luftdruckänderung, 6 Std.",
    "Pressure change, 12 h": "Luftdruckänderung, 12 Std.",

    # ── Current conditions ─────────────────────────────────────────────────
    "Temperature": "Temperatur",
    "Humidity": "Luftfeuchtigkeit",
    "Pressure": "Luftdruck",
    "Feels like {value}°C": "Gefühlt {value} °C",
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
    "The charting library did not load, so the graphs are missing.":
        "Die Diagrammbibliothek wurde nicht geladen, daher fehlen die Grafiken.",

    # Read aloud in place of the chart, never shown. A screen reader finds a
    # bare <canvas> and says nothing at all, so these carry what a summary
    # can carry: which measurement, over what, and how far it ranged.
    "{metric} over {period}: {min} to {max} {unit}":
        "{metric} über {period}: {min} bis {max} {unit}",
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
