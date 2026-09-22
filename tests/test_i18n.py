"""Tests for the English/German page.

Two of these are worth more than the rest: :class:`TestCatalogueCoverage`
scans the template and the ES modules for the strings they actually ask for
and fails if one has no German, and :class:`TestGermanPage` renders the whole
page and fails if English leaks through. Between them, a string added without
a translation cannot reach the site quietly.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

import pytest

from app import clock, i18n, nowcast, weather

PACKAGE = Path(i18n.__file__).parent
TEMPLATE = PACKAGE / "templates" / "index.html"
JS_DIR = PACKAGE / "static" / "js"

#: ``t('…')`` / ``t("…")`` with a literal argument. Calls with a variable —
#: ``t(forecast)``, ``t(row[0])`` — are covered by the tests below that walk
#: the phrases those variables can hold.
T_CALL = re.compile(r"""(?<![\w.])t\(\s*(['"])((?:\\.|(?!\1).)*)\1""")


def literals(path: Path) -> set[str]:
    return {m.group(2) for m in T_CALL.finditer(path.read_text())}


class TestNegotiation:
    @pytest.mark.parametrize("header,expected", [
        (None, "en"),
        ("", "en"),
        ("en-US,en;q=0.9", "en"),
        ("de-DE,de;q=0.9,en;q=0.8", "de"),
        ("de", "de"),
        ("de-AT", "de"),
        ("de-CH,de;q=0.9", "de"),
        ("fr-FR,fr;q=0.9", "en"),
        ("*", "en"),
    ])
    def test_picks_the_language_the_browser_asked_for(self, header, expected):
        assert i18n.negotiate(header) == expected

    def test_quality_beats_position(self):
        """A browser listing French first and German second still gets German."""
        assert i18n.negotiate("fr;q=1.0,en;q=0.5,de;q=0.9") == "de"

    def test_equal_quality_keeps_the_browser_s_order(self):
        assert i18n.negotiate("de,en") == "de"
        assert i18n.negotiate("en,de") == "en"

    def test_an_explicitly_refused_language_is_not_chosen(self):
        assert i18n.negotiate("de;q=0, en;q=0.5") == "en"

    def test_malformed_quality_does_not_raise(self):
        assert i18n.negotiate("de;q=banana,en") == "en"

    def test_override_wins(self):
        assert i18n.negotiate("de-DE,de;q=0.9", "en") == "en"
        assert i18n.negotiate("en-GB", "de") == "de"

    def test_an_unsupported_override_falls_back_to_the_header(self):
        assert i18n.negotiate("de-DE", "fr") == "de"


class TestNumbers:
    @pytest.mark.parametrize("lang,expected", [("en", "22.5"), ("de", "22,5")])
    def test_decimal_separator_follows_the_language(self, lang, expected):
        assert i18n.format_number(22.5, 1, lang) == expected

    @pytest.mark.parametrize("lang,expected", [("en", "25,783"), ("de", "25.783")])
    def test_thousands_separator_follows_the_language(self, lang, expected):
        assert i18n.format_number(25783, 0, lang, grouping=True) == expected

    def test_both_separators_at_once(self):
        """The naive two-pass swap would turn 25.783,5 into 25,783,5."""
        assert i18n.format_number(25783.5, 1, "de", grouping=True) == "25.783,5"

    @pytest.mark.parametrize("value,expected", [(1.4, "+1,4"), (-0.3, "-0,3"), (0, "+0,0")])
    def test_signed_values(self, value, expected):
        assert i18n.format_number(value, 1, "de", sign=True) == expected

    def test_none_is_empty(self):
        assert i18n.format_number(None, 1, "de") == ""

    def test_an_unknown_language_formats_like_english(self):
        assert i18n.format_number(22.5, 1, "fr") == "22.5"


class TestTranslator:
    def test_english_returns_the_source_string(self):
        t = i18n.translator("en")
        assert t("Rain likely") == "Rain likely"

    def test_german_translates(self):
        assert i18n.translator("de")("Rain likely") == "Regen wahrscheinlich"

    def test_an_unknown_string_returns_itself(self):
        """A string added to the page before the catalogue stays readable."""
        assert i18n.translator("de")("Brand new label") == "Brand new label"

    def test_placeholders_are_filled(self):
        t = i18n.translator("de")
        assert t("in {hours} h", hours=6) == "in 6 Std."

    def test_german_may_reorder_placeholders(self):
        assert i18n.translator("de")("{year} average", year=2026) == "Mittel 2026"


class TestDateFormatting:
    """Dates are reformatted from the stored string, never parsed.

    A stored timestamp is a naive local wall clock. Turning it into a
    datetime to print it would invite exactly the timezone confusion the
    rest of the codebase is careful to avoid.
    """

    @pytest.mark.parametrize("lang,expected", [
        ("en", "21 Sep 2026"),
        ("de", "21.09.2026"),
    ])
    def test_a_date_is_written_the_way_the_language_writes_it(self, lang, expected):
        assert i18n.format_date("2026-09-21 21:50:00", lang) == expected

    def test_the_date_half_alone_is_enough(self):
        assert i18n.format_date("2026-09-21", "de") == "21.09.2026"

    def test_a_leading_zero_is_dropped_in_english_only(self):
        assert i18n.format_date("2026-09-01", "en") == "1 Sep 2026"
        assert i18n.format_date("2026-09-01", "de") == "01.09.2026"

    @pytest.mark.parametrize("lang,expected", [
        ("en", "21 Sep 2026, 21:50"),
        ("de", "21.09.2026, 21:50"),
    ])
    def test_the_clock_stays_24_hour_in_both(self, lang, expected):
        assert i18n.format_datetime("2026-09-21 21:50:00", lang) == expected

    @pytest.mark.parametrize("value", ["", "not a date", "2026"])
    def test_something_unparseable_comes_back_untouched(self, value):
        assert i18n.format_date(value, "de") == value


class TestRelativeTime:
    @pytest.mark.parametrize("seconds,en,de", [
        (0, "just now", "gerade eben"),
        (59, "just now", "gerade eben"),
        (60, "1 minute ago", "vor 1 Minute"),
        (119, "1 minute ago", "vor 1 Minute"),
        (120, "2 minutes ago", "vor 2 Minuten"),
        (3599, "59 minutes ago", "vor 59 Minuten"),
        (3600, "1 hour ago", "vor 1 Stunde"),
        (7200, "2 hours ago", "vor 2 Stunden"),
        (86_399, "23 hours ago", "vor 23 Stunden"),
    ])
    def test_it_counts_and_pluralises(self, seconds, en, de):
        assert i18n.format_relative(seconds, "en") == en
        assert i18n.format_relative(seconds, "de") == de

    def test_a_day_or_more_declines_to_answer(self):
        """Past a day the caller shows the date instead — "29 hours ago" is
        not what anyone wants to read."""
        assert i18n.format_relative(86_400, "en") is None
        assert i18n.format_relative(500_000, "en") is None

    def test_a_clock_slightly_ahead_reads_as_now(self):
        """A reading posted with a future timestamp, or a host clock a second
        fast, must not produce "-1 minutes ago"."""
        assert i18n.format_relative(-30, "en") == "just now"


class TestTheAgeComesFromTheServer:
    """The browser must not compute it from the stored timestamp.

    It is a naive local wall clock, so a reader in another timezone would
    have their browser read it as their own local time — making a reading
    from a minute ago look an hour old.
    """

    async def test_status_carries_the_age(self, client):
        await client.post("/api/weather", json={
            "temperature": "20", "humidity": "55", "pressure": "1013",
            "timestamp": clock.fmt_ts(clock.now() - timedelta(minutes=3)),
        })
        body = (await client.get("/api/weather/status")).json()
        assert 170 <= body["age_seconds"] <= 190

    def test_the_poller_reads_it_rather_than_subtracting(self):
        source = (JS_DIR / "poll.js").read_text()
        assert "age_seconds" in source


class TestCatalogueCoverage:
    """Every string the page asks for must have a German translation."""

    def test_the_template_is_fully_translated(self):
        missing = sorted(literals(TEMPLATE) - set(i18n.GERMAN))
        assert not missing, f"no German for: {missing}"

    @pytest.mark.parametrize("name", sorted(p.name for p in JS_DIR.glob("*.js")))
    def test_the_modules_are_fully_translated(self, name):
        missing = sorted(literals(JS_DIR / name) - set(i18n.GERMAN))
        assert not missing, f"no German in {name} for: {missing}"

    def test_every_forecast_phrase_is_translated(self):
        """These reach t() as a variable, so the scan above cannot see them."""
        phrases = set(re.findall(r'return "([^"]+)"', weather.__file__ and
                                 Path(weather.__file__).read_text()))
        phrases.add(weather.NO_DATA)
        missing = sorted(phrases - set(i18n.GERMAN))
        assert not missing, f"no German for forecast phrase: {missing}"

    def test_every_nowcast_label_is_translated(self):
        labels = {nowcast.describe(p / 100, 0.3) for p in range(0, 101, 5)}
        missing = sorted(labels - set(i18n.GERMAN))
        assert not missing, f"no German for nowcast label: {missing}"

    def test_the_record_row_labels_are_translated(self):
        """Passed as macro arguments, so also invisible to the scan."""
        labels = set(re.findall(r"extreme_row\(\s*\"([^\"]+)\"", TEMPLATE.read_text()))
        labels |= set(re.findall(r"\(\s*'([A-Z][^']*\(avg\))'", TEMPLATE.read_text()))
        assert labels, "expected to find the record labels in the template"
        missing = sorted(labels - set(i18n.GERMAN))
        assert not missing, f"no German for record label: {missing}"

    def test_every_rule_ladder_row_is_translated(self):
        """The ladder reaches t() as tier.condition, so the scan cannot see it."""
        strings = set()
        for tier in weather.RULE_LADDER:
            strings.add(tier.condition)
            if tier.note:
                strings.add(tier.note)
        missing = sorted(strings - set(i18n.GERMAN))
        assert not missing, f"no German for rule ladder row: {missing}"

    def test_every_learned_ladder_row_is_translated(self):
        """The composed ladder reaches t() as tier.condition too, and its rows
        are the ones a reader sees once the sky model has shipped."""
        strings = set()
        for tier in weather.learned_ladder(0.212):
            strings.add(tier.condition)
            if tier.note:
                strings.add(tier.note)
        missing = sorted(strings - set(i18n.GERMAN))
        assert not missing, f"no German for learned ladder row: {missing}"

    def test_every_phrase_the_composed_ladder_can_produce_is_translated(self):
        """Including the two it reaches through humidity rather than a rung of
        their own, which the ladder itself therefore does not name."""
        phrases = {tier.phrase for tier in weather.learned_ladder(0.212)}
        phrases |= {"Settled but humid", "Overcast and humid"}
        missing = sorted(phrases - set(i18n.GERMAN))
        assert not missing, f"no German for composed forecast phrase: {missing}"

    def test_every_sky_wording_is_translated(self):
        """describe_sky() returns these as message ids, like the phrases."""
        words = {nowcast.describe_sky(p) for p in (0.05, 0.45, 0.95)}
        missing = sorted(words - set(i18n.GERMAN))
        assert not missing, f"no German for sky wording: {missing}"

    def test_every_feature_label_is_translated(self):
        """Also passed as a variable — the labels come from the model file."""
        labels = {f.label for f in nowcast.FEATURE_FORMATS.values()}
        missing = sorted(labels - set(i18n.GERMAN))
        assert not missing, f"no German for feature label: {missing}"

    def test_the_catalogue_has_no_empty_translations(self):
        assert not [k for k, v in i18n.GERMAN.items() if not v.strip()]

    def test_placeholders_survive_translation(self):
        """A dropped {name} would render the sentence with a hole in it."""
        placeholders = re.compile(r"\{(\w+)\}")
        for source, translated in i18n.GERMAN.items():
            assert set(placeholders.findall(source)) == set(placeholders.findall(translated)), source


class TestPagePayload:
    def test_english_ships_an_empty_catalogue(self):
        """English is the message id, so there is nothing to send."""
        assert i18n.page_payload("en")["strings"] == {}

    def test_german_ships_the_catalogue(self):
        payload = i18n.page_payload("de")
        assert payload["strings"]["Rain likely"] == "Regen wahrscheinlich"
        assert payload["decimal"] == ","
        assert payload["thousands"] == "."
        assert payload["months_long"][0] == "Januar"
        assert payload["days_short"][0] == "Mo"

    def test_the_payload_is_json_serialisable(self):
        json.dumps(i18n.page_payload("de"))


async def seed(client, hours: int = 3) -> None:
    now = clock.now().replace(second=0, microsecond=0, tzinfo=None)
    now -= timedelta(minutes=now.minute % 5)
    for i in range(hours * 12):
        moment = now - timedelta(minutes=5 * i)
        await client.post("/api/weather", json={
            "temperature": "21.5", "humidity": "62", "pressure": "1013.4",
            "timestamp": moment.strftime(clock.TS_FORMAT),
        })


GERMAN_HEADERS = {"Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}

CATALOGUE_BLOB = re.compile(
    r'<script id="i18n-data" type="application/json">.*?</script>', re.S)


def visible(text: str) -> str:
    """The page without the embedded catalogue.

    That blob is keyed by the English source strings, so it contains every
    English phrase by construction — checking it for English leaks would
    always fail.
    """
    return CATALOGUE_BLOB.sub("", text)

#: Strings that only appear on an untranslated page. If one of these turns up
#: in the German render, something is going out in the wrong language.
ENGLISH_GIVEAWAYS = (
    "Balcony Weather", "Live from the balcony", "Temperature", "Humidity",
    "Pressure", "Feels like", "Dew point", "Last updated", "24 Hours",
    "All Time", "Today's Records", "All-Time Records", "Hottest", "Coldest",
    "Most humid", "Readings", "Temperature Calendar", "Previous month",
    "Cold", "How these two predictions work", "The outlook", "Why both",
    "The technical details", "Rain followed", "What is driving the number",
)


class TestGermanPage:
    async def test_a_german_browser_gets_german(self, client):
        await seed(client)
        text = (await client.get("/", headers=GERMAN_HEADERS)).text
        assert 'lang="de"' in text
        assert "Balkon-Wetter" in text
        assert "Luftfeuchtigkeit" in text
        assert "Temperaturkalender" in text

    async def test_no_english_leaks_into_the_german_page(self, client):
        await seed(client)
        text = visible((await client.get("/", headers=GERMAN_HEADERS)).text)
        leaked = [s for s in ENGLISH_GIVEAWAYS if s in text]
        assert not leaked, f"untranslated on the German page: {leaked}"

    async def test_numbers_use_a_decimal_comma(self, client):
        await seed(client)
        text = visible((await client.get("/", headers=GERMAN_HEADERS)).text)
        assert "21,5" in text
        assert "21.5" not in text

    async def test_an_english_browser_still_gets_english(self, client):
        await seed(client)
        text = (await client.get("/", headers={"Accept-Language": "en-GB,en;q=0.9"})).text
        assert 'lang="en"' in text
        assert "Balcony Weather" in text
        assert "21.5" in text

    async def test_the_default_is_english(self, client):
        await seed(client)
        assert 'lang="en"' in (await client.get("/")).text

    async def test_the_query_override_wins(self, client):
        await seed(client)
        text = (await client.get("/?lang=de", headers={"Accept-Language": "en"})).text
        assert 'lang="de"' in text
        text = (await client.get("/?lang=en", headers=GERMAN_HEADERS)).text
        assert 'lang="en"' in text

    async def test_the_page_says_it_varies_by_language(self, client):
        """Membership, not equality: compression correctly adds its own key.

        A cache must vary on Accept-Encoding as well, so GZipMiddleware
        appends it. What matters here is that Accept-Language is in the list.
        """
        vary = (await client.get("/")).headers["vary"]
        assert "Accept-Language" in [key.strip() for key in vary.split(",")]

    async def test_the_empty_state_is_translated(self, client):
        text = visible((await client.get("/", headers=GERMAN_HEADERS)).text)
        assert "Warte auf den ersten Messwert" in text
        assert "Waiting for first weather reading" not in text

    async def test_the_browser_gets_the_catalogue_the_render_used(self, client):
        """The poller rewrites what the render produced, so it needs the same
        strings — otherwise the page turns half-English after sixty seconds."""
        await seed(client)
        text = (await client.get("/", headers=GERMAN_HEADERS)).text
        blob = re.search(
            r'<script id="i18n-data" type="application/json">(.*?)</script>', text, re.S)
        assert blob, "the page did not embed its catalogue"
        payload = json.loads(blob.group(1).replace("&#34;", '"').replace("&amp;", "&"))
        assert payload["lang"] == "de"
        assert payload["strings"]["Rain likely"] == "Regen wahrscheinlich"


class TestApiStaysEnglish:
    """The JSON API is an interface, not a page; its phrases are identifiers.

    ``weather.forecast_emoji()`` matches on them, ``ml/train.py`` replays
    against them and the tests compare them. Translating them at the API would
    make all three language-dependent, so the browser translates instead.
    """

    async def test_status_reports_the_canonical_phrase(self, client):
        await seed(client)
        body = (await client.get("/api/weather/status", headers=GERMAN_HEADERS)).json()
        assert body["forecast"] in set(i18n.GERMAN) | {None}
        assert body["forecast"] not in i18n.GERMAN.values()


class TestNowcastPillStructure:
    """The pill is built twice — by Jinja and by the poller — and the two must
    agree on its shape, not just its words.

    It was an ``inline-flex`` row, which makes the label, the percentage and
    the horizon three side-by-side columns that each wrap inside themselves.
    English was short enough to hide it; German put the "·" alone on one line
    and split "in 6 Std." down the middle on a phone.
    """

    CSS = PACKAGE / "static" / "css" / "dashboard.css"

    def test_the_template_keeps_the_chance_in_one_span(self):
        assert 'class="nowcast-chance"' in TEMPLATE.read_text()

    def test_the_poller_builds_the_same_span(self):
        assert "nowcast-chance" in (JS_DIR / "poll.js").read_text()

    def test_the_stylesheet_stops_the_chance_wrapping(self):
        rule = re.search(r"\.nowcast-chance\s*\{([^}]*)\}", self.CSS.read_text())
        assert rule, "no .nowcast-chance rule"
        assert "nowrap" in rule.group(1)

    def test_the_pill_is_not_a_flex_row(self):
        rule = re.search(r"\.banner-nowcast\s*\{([^}]*)\}", self.CSS.read_text())
        assert rule, "no .banner-nowcast rule"
        assert "flex" not in rule.group(1), "flex items wrap internally; see the docstring"


class TestDeepDiveStructure:
    """The explainer's tables are written by Jinja and rewritten by poll.js.

    They are the same hooks in both, so the contract is the attribute names.
    A rename on one side alone leaves a section that is correct on load and
    silently frozen a minute later, which is the failure this catches.
    """

    POLL = JS_DIR / "poll.js"

    @pytest.mark.parametrize("hook", [
        "deep-outlook-live",   # the live pressure rank
        "deep-features",       # the contribution table
        'data-cell="intercept"',
        'data-cell="total"',
        "rule-ladder",         # the rung the outlook is standing on
        "deep-sky-live",       # the cloud model's live probability
    ])
    def test_both_sides_name_the_same_hook(self, hook):
        assert hook in TEMPLATE.read_text(), f"{hook} missing from the template"
        assert hook in self.POLL.read_text(), f"{hook} missing from poll.js"

    async def test_the_ladder_rows_carry_the_phrase_the_api_sends(self, client):
        """The poller matches data-phrase against status.forecast, which is
        English whatever the page is in — so the German render must still
        carry the English identifier in the attribute."""
        await seed(client)
        text = (await client.get("/", headers=GERMAN_HEADERS)).text
        phrases = set(re.findall(r'data-phrase="([^"]+)"', text))
        assert phrases == {tier.phrase for tier in weather.RULE_LADDER}

    async def test_the_rendered_ladder_marks_the_current_rung(self, client):
        """Exactly one rung is highlighted, and it is the one the banner says."""
        await seed(client)
        body = (await client.get("/api/weather/status")).json()
        text = (await client.get("/")).text
        marked = re.findall(r'data-phrase="([^"]+)" class="is-active"', text)
        if body["forecast"] in {tier.phrase for tier in weather.RULE_LADDER}:
            assert marked == [body["forecast"]]
        else:
            assert marked == []

    def test_the_live_line_carries_the_window_it_describes(self):
        """poll.js reads the day count off the element rather than hard-coding it."""
        assert 'data-days="{{ percentile_days }}"' in TEMPLATE.read_text()
        assert "dataset.days" in self.POLL.read_text()

    def test_the_weight_colours_are_not_the_temperature_ones(self):
        """delta-up/-down mean warmer and cooler; these mean more and less rain."""
        css = (PACKAGE / "static" / "css" / "dashboard.css").read_text()
        assert ".feature-table .weight-up" in css
        assert ".feature-table .weight-down" in css
        assert "delta-up" not in re.search(
            r"function updateFeatureTable.*?\n}", self.POLL.read_text(), re.S).group(0)

    def test_wide_tables_scroll_inside_their_own_box(self):
        """A table wider than the phone must not push the whole page sideways."""
        css = (PACKAGE / "static" / "css" / "dashboard.css").read_text()
        rule = re.search(r"\.table-scroll\s*\{([^}]*)\}", css)
        assert rule, "no .table-scroll rule"
        assert "auto" in rule.group(1)
        assert TEMPLATE.read_text().count('class="table-scroll"') >= 3


class TestPagerStructure:
    """The calendar and the climate year page the same way, from one module.

    These are cheap structural guards on an arrangement that is easy to undo
    by accident: a copied pager, or markup that drops the shared classes and
    silently loses the snap behaviour on one of the two.
    """

    CSS = PACKAGE / "static" / "css" / "dashboard.css"

    def test_both_tracks_carry_the_shared_classes(self):
        markup = TEMPLATE.read_text()
        for hook in ('class="pager-track heatmap-track"', 'class="pager-track climate-track"',
                     'class="pager-page climate-year"'):
            assert hook in markup, hook
        assert "pager-page heatmap-month" in (JS_DIR / "heatmap.js").read_text()

    def test_both_modules_use_the_shared_pager(self):
        for name in ("heatmap.js", "climate.js"):
            assert "createPager" in (JS_DIR / name).read_text(), name

    def test_only_the_pager_module_interpolates_the_track_height(self):
        """A second copy of this is the thing worth preventing."""
        owners = [p.name for p in JS_DIR.glob("*.js") if "scrollLeft / track.clientWidth" in p.read_text()]
        assert owners == ["pager.js"], owners

    def test_the_hover_grow_stays_inside_the_gutter(self):
        """scale(1.04) on a ~117px cell is 2.3px a side; the half-gap is 3px."""
        css = self.CSS.read_text()
        scale = re.search(r"\.heatmap-cell:hover\s*\{[^}]*scale\(([\d.]+)\)", css)
        assert scale, "no hover scale rule"
        grid = re.search(r"\.heatmap-grid\s*\{([^}]*)\}", css)
        gap = re.search(r"gap:\s*(\d+)px", grid.group(1))
        assert gap, "no grid gap"
        widest = 117.0            # a 900px container, less the card's padding
        assert (float(scale.group(1)) - 1) / 2 * widest < int(gap.group(1)) / 2, css


class TestOneExplainerCard:
    """The short answer and the working share one card, one handle."""

    def test_the_predictions_hold_a_single_details(self):
        markup = TEMPLATE.read_text()
        assert markup.count('<details class="prediction-note">') == 1
        assert '<details class="prediction-note prediction-deep">' not in markup

    def test_the_deep_dive_is_nested_and_has_no_card_of_its_own(self):
        markup = TEMPLATE.read_text()
        body = markup.index('<div class="prediction-note-body">')
        deep = markup.index('<details class="prediction-deep">')
        close = markup.index("</details>", deep)
        assert body < deep < close
        assert markup.count('class="prediction-note-body"') == 1
        assert 'class="prediction-deep-body"' in markup

    def test_the_card_handle_rules_do_not_reach_the_nested_one(self):
        """A descendant selector would give the inner summary the ⓘ too."""
        css = (PACKAGE / "static" / "css" / "dashboard.css").read_text()
        assert re.search(r"\.prediction-note > summary::before\s*\{[^}]*content", css)
        assert not re.search(r"\.prediction-note summary::before", css)


class TestClimateFollowsTheYear:
    """The chart and the year label show the same year.

    Before, the chart plotted the climatology pooled over every year while a
    table under it showed one year at a time, so the same month had two
    different numbers on one card depending on where you looked.
    """

    def test_the_render_embeds_every_year_not_the_pooled_average(self):
        markup = TEMPLATE.read_text()
        assert "climate.monthly_by_year | tojson" in markup
        # The pooled climatology may appear only as the band drawn behind
        # the year — never as a series the year label claims to describe.
        pooled = re.findall(
            r'id="([a-z-]+)" type="application/json">\{\{ climate\.monthly_all',
            markup,
        )
        assert pooled in ([], ["climate-norm"]), pooled

    def test_the_band_is_the_archive_behind_the_year(self):
        source = (JS_DIR / "climate.js").read_text()
        assert "climate-norm" in source
        # Drawn first, so the year is on top of its own context.
        assert "[...bandDatasets()" in source

    def test_the_month_table_is_gone_from_the_page(self):
        markup = TEMPLATE.read_text()
        assert "month-columns" not in markup
        assert "months[month.month - 1]" not in markup

    def test_the_year_page_still_carries_the_yearly_summary(self):
        markup = TEMPLATE.read_text()
        assert "{year} average" in markup
        assert "min {min} max {max}" in markup

    def test_the_chart_redraws_as_the_page_turns(self):
        source = (JS_DIR / "climate.js").read_text()
        assert "onChange" in source and "showYear" in source
        # Swapped in place: destroying and recreating resizes the canvas.
        assert "chart.data.datasets =" in source

    def test_the_axis_is_shared_across_years(self):
        """Refitting per year would draw a mild year and a harsh one alike."""
        source = (JS_DIR / "climate.js").read_text()
        assert "sharedAxis" in source
        assert "suggestedMin" in source and "suggestedMax" in source
