"""End-to-end tests through the ASGI app."""

import re
from datetime import timedelta
from pathlib import Path

import pytest

from app import clock

READING = {"temperature": "21.5", "humidity": "55", "pressure": "1013",
           "timestamp": "2026-06-20T21:00:00"}


def at(minutes_ago: int, **overrides) -> dict:
    moment = clock.now().replace(second=0, microsecond=0) - timedelta(minutes=minutes_ago)
    return {**READING, "timestamp": moment.strftime("%Y-%m-%dT%H:%M:00"), **overrides}


@pytest.fixture
async def built_client(_isolated_settings, monkeypatch):
    """A client for an app built the way a deployed image is: with a GIT_SHA.

    Asset caching is the one behaviour that differs between a built image and
    a checkout, so it cannot be tested through the ordinary fixture.
    """
    import httpx

    from app.config import get_settings

    monkeypatch.setenv("GIT_SHA", "0123456789abcdef0123456789abcdef01234567")
    get_settings.cache_clear()

    from app.main import create_app

    application = create_app()
    transport = httpx.ASGITransport(app=application)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
        application.router.lifespan_context(application),
    ):
        yield c
    get_settings.cache_clear()


class TestIngestion:
    async def test_accepts_a_reading(self, client):
        response = await client.post("/api/weather", json=READING)
        assert response.status_code == 200
        assert response.json()["timestamp"] == "2026-06-20 21:00:00"

    @pytest.mark.parametrize("payload,reason", [
        ({"temperature": "20"}, "missing fields"),
        ({**READING, "temperature": "unavailable"}, "Home Assistant sends this when a sensor drops out"),
        ({**READING, "temperature": "9999"}, "sensor glitch"),
        ({**READING, "humidity": "500"}, "impossible humidity"),
        ({**READING, "pressure": "-5"}, "impossible pressure"),
        ({**READING, "timestamp": "not-a-date"}, "unsortable timestamp"),
        ({**READING, "timestamp": ""}, "empty timestamp"),
    ])
    async def test_rejects_bad_payloads(self, client, payload, reason):
        response = await client.post("/api/weather", json=payload)
        assert response.status_code == 422, f"should reject: {reason}"
        assert not (await client.get("/api/weather/current")).is_success

    async def test_rejected_readings_are_not_stored(self, client):
        await client.post("/api/weather", json=READING)
        await client.post("/api/weather", json={**READING, "temperature": "9999",
                                                "timestamp": "2026-06-20T21:05:00"})
        stats = (await client.get("/api/weather/stats?period=all")).json()
        assert stats["count"] == 1
        assert stats["temperature"]["max"] == 21.5


class TestAuth:
    @pytest.fixture
    def secured(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "s3cret")
        from app.config import get_settings
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    async def test_open_when_no_key_configured(self, client):
        assert (await client.post("/api/weather", json=READING)).status_code == 200

    async def test_rejects_missing_key(self, secured, client):
        assert (await client.post("/api/weather", json=READING)).status_code == 401

    async def test_rejects_wrong_key(self, secured, client):
        response = await client.post("/api/weather", json=READING,
                                     headers={"X-API-Key": "wrong"})
        assert response.status_code == 401

    async def test_accepts_correct_key(self, secured, client):
        response = await client.post("/api/weather", json=READING,
                                     headers={"X-API-Key": "s3cret"})
        assert response.status_code == 200

    async def test_cleanup_is_guarded_too(self, secured, client):
        assert (await client.delete("/api/weather/cleanup")).status_code == 401

    async def test_reads_stay_public(self, secured, client):
        assert (await client.get("/api/weather/stats")).status_code == 200


class TestHealthz:
    """The probe doubles as "which build is this?", so it is worth pinning."""

    async def test_reports_the_commit_it_was_built_from(self, client, monkeypatch):
        from app.config import get_settings

        monkeypatch.setenv("GIT_SHA", "abc123def456")
        get_settings.cache_clear()
        try:
            body = (await client.get("/healthz")).json()
            assert body["commit"] == "abc123def456"
            assert body["status"] == "ok"
        finally:
            get_settings.cache_clear()

    async def test_says_unknown_outside_a_built_image(self, client):
        """Locally there is no commit, and claiming one would be worse."""
        body = (await client.get("/healthz")).json()
        assert body["commit"] == "unknown"


class TestReadEndpoints:
    async def test_empty_database_returns_404_not_a_body_with_error(self, client):
        assert (await client.get("/api/weather/current")).status_code == 404
        assert (await client.get("/api/weather/status")).status_code == 404

    async def test_status_reports_everything_the_dashboard_needs(self, client):
        for minutes in range(0, 60, 5):
            await client.post("/api/weather", json=at(minutes))
        status = (await client.get("/api/weather/status")).json()
        assert set(status) >= {"current", "dew_point", "heat_index", "pressure_trend",
                               "forecast", "forecast_emoji", "frost_warning",
                               "yesterday", "stale", "comparison_hours"}
        assert status["stale"] is False

    async def test_status_flags_a_dead_feed(self, client):
        await client.post("/api/weather", json=at(minutes_ago=180))
        assert (await client.get("/api/weather/status")).json()["stale"] is True

    async def test_history_shape(self, client):
        await client.post("/api/weather", json=at(0))
        body = (await client.get("/api/weather/history?period=24h")).json()
        assert set(body) == {
            "readings", "interval_seconds", "bucketed", "expected_samples",
        }
        assert body["bucketed"] is False

    @pytest.mark.parametrize("period", ["3h", "24h", "7d", "30d", "today", "all"])
    async def test_every_period_is_accepted(self, client, period):
        await client.post("/api/weather", json=at(0))
        assert (await client.get(f"/api/weather/history?period={period}")).status_code == 200
        assert (await client.get(f"/api/weather/stats?period={period}")).status_code == 200

    async def test_unknown_period_is_rejected(self, client):
        assert (await client.get("/api/weather/history?period=99y")).status_code == 422

    @pytest.mark.parametrize("months,status", [(0, 422), (1, 200), (24, 200), (25, 422)])
    async def test_daily_months_bounds(self, client, months, status):
        assert (await client.get(f"/api/weather/daily?months={months}")).status_code == status

    async def test_healthz(self, client):
        body = (await client.get("/healthz")).json()
        assert body["status"] == "ok"
        assert body["latest_reading"] is None
        await client.post("/api/weather", json=READING)
        assert (await client.get("/healthz")).json()["latest_reading"] == "2026-06-20 21:00:00"


class TestCleanupEndpoint:
    async def test_off_grid_mode(self, client):
        await client.post("/api/weather", json={**READING, "timestamp": "2026-06-20T21:00:00"})
        await client.post("/api/weather", json={**READING, "timestamp": "2026-06-20T21:07:00"})
        response = await client.delete("/api/weather/cleanup")
        assert response.json() == {"status": "ok", "deleted": 1}

    async def test_range_mode(self, client):
        for minute in (0, 5, 10):
            await client.post("/api/weather", json={**READING, "timestamp": f"2026-06-20T21:{minute:02d}:00"})
        response = await client.delete(
            "/api/weather/cleanup?from=2026-06-20T21:00:00&to=2026-06-20T21:05:00")
        assert response.json()["deleted"] == 2

    async def test_half_a_range_is_a_bad_request(self, client):
        assert (await client.delete("/api/weather/cleanup?from=2026-06-20T21:00:00")).status_code == 400

    async def test_unparseable_range_is_a_bad_request(self, client):
        response = await client.delete("/api/weather/cleanup?from=nope&to=also-nope")
        assert response.status_code == 400


class TestDashboard:
    async def test_empty_state_renders(self, client):
        response = await client.get("/")
        assert response.status_code == 200
        assert "Waiting for first weather reading" in response.text

    async def test_page_is_not_cached(self, client):
        assert (await client.get("/")).headers["cache-control"] == "no-store"

    async def test_the_page_is_compressed(self, client):
        """The German page is 52 KB of mostly repeated prose; send it small.

        httpx asks for gzip and decodes it transparently, so this reads the
        header rather than the body length.
        """
        for minutes in range(0, 120, 5):
            await client.post("/api/weather", json=at(minutes))
        response = await client.get("/", headers={"Accept-Language": "de"})
        assert response.headers.get("content-encoding") == "gzip"

    async def test_the_chart_series_is_compressed(self, client):
        """The largest response the dashboard asks for, and the most
        compressible: a few thousand rows of near-identical numbers."""
        for minutes in range(0, 60 * 24, 5):
            await client.post("/api/weather", json=at(minutes))
        response = await client.get("/api/weather/history?period=24h")
        assert response.headers.get("content-encoding") == "gzip"

    async def test_renders_current_conditions(self, client):
        for minutes in range(0, 120, 5):
            await client.post("/api/weather", json=at(minutes, temperature="21.5"))
        text = (await client.get("/")).text
        assert "21.5" in text
        assert "Balcony Weather" in text
        assert 'id="val-temp"' in text

    async def test_static_assets_are_served(self, client):
        for path in ("/static/css/dashboard.css", "/static/js/main.js",
                     "/static/js/charts.js", "/static/icons/favicon.svg"):
            assert (await client.get(path)).status_code == 200, path

    async def test_page_references_its_assets(self, client):
        text = (await client.get("/")).text
        assert "/css/dashboard.css" in text
        assert "/js/main.js" in text

    async def test_local_asset_urls_are_root_relative(self, client):
        """An absolute http:// asset URL is blocked as mixed content behind an
        HTTPS proxy, leaving the page with no stylesheet and no scripts."""
        response = await client.get("/", headers={
            "X-Forwarded-Proto": "https", "X-Forwarded-Host": "weather.example.com"})
        local = re.findall(r'(?:href|src)="([^"]*static[^"]*)"', response.text)
        assert local, "expected the page to reference its own assets"
        for url in local:
            assert url.startswith("/static/"), f"{url} is not root-relative"

    async def test_no_absolute_urls_point_back_at_the_test_host(self, client):
        text = (await client.get("/")).text
        assert "http://test/" not in text

    async def test_assets_are_cache_busted_by_version(self, client):
        from app.config import get_settings

        version = get_settings().asset_version
        text = (await client.get("/")).text
        assert f"/static/{version}/js/main.js" in text
        assert f"/static/{version}/css/dashboard.css" in text

    async def test_the_versioned_prefix_serves_the_same_files(self, client):
        from app.config import get_settings

        version = get_settings().asset_version
        for path in ("css/dashboard.css", "js/main.js", "js/pager.js"):
            assert (await client.get(f"/static/{version}/{path}")).status_code == 200, path

    async def test_versioned_assets_are_cached_forever(self, built_client):
        """The path names the build, so the bytes behind it cannot change."""
        from app.config import get_settings

        version = get_settings().asset_version
        response = await built_client.get(f"/static/{version}/js/main.js")
        assert response.headers["cache-control"] == (
            "public, max-age=31536000, immutable"
        )

    async def test_the_unversioned_mount_still_revalidates(self, built_client):
        """It serves the same file at a path that does not name a build, so
        promising the browser it never changes would be a lie."""
        response = await built_client.get("/static/js/main.js")
        assert response.status_code == 200
        assert "immutable" not in response.headers.get("cache-control", "")

    async def test_a_revalidated_asset_keeps_its_caching(self, built_client):
        """A 304 that dropped Cache-Control would re-arm the round trip it
        is meant to remove."""
        from app.config import get_settings

        version = get_settings().asset_version
        first = await built_client.get(f"/static/{version}/js/main.js")
        again = await built_client.get(
            f"/static/{version}/js/main.js",
            headers={"If-None-Match": first.headers["etag"]},
        )
        assert again.status_code == 304
        assert "immutable" in again.headers.get("cache-control", "")

    async def test_an_unbuilt_checkout_does_not_promise_a_year(self, client):
        """Without GIT_SHA the version is __version__, which does not move
        between releases — exactly the state that made browsers serve
        pre-release assets. Caching it forever would restage that bug."""
        from app.config import get_settings

        settings = get_settings()
        assert not settings.asset_version_names_a_build
        response = await client.get(f"/static/{settings.asset_version}/js/main.js")
        assert response.status_code == 200
        assert "immutable" not in response.headers.get("cache-control", "")

    async def test_every_module_the_page_loads_is_versioned(self, client):
        """Resolve the import graph the way the browser does.

        A ``?v=`` query versions only the URL the page hands out. The entry
        module's ``./heatmap.js`` resolves against *that* URL, so with the
        query scheme it came out unversioned and the browser went on serving
        whatever it had cached — a page with new markup and months-old
        modules, which renders wrong rather than merely stale. Versioning the
        directory fixes it because relative imports inherit the directory,
        and this walks the graph to prove they do.
        """
        from app.config import get_settings

        version = get_settings().asset_version
        text = (await client.get("/")).text
        entry = re.search(r'<script type="module" src="([^"]+)"', text)
        assert entry, "no module entry point on the page"

        seen, queue = set(), [entry.group(1)]
        while queue:
            url = queue.pop()
            if url in seen:
                continue
            seen.add(url)
            assert f"/static/{version}/" in url, f"{url} is not versioned"
            response = await client.get(url)
            assert response.status_code == 200, url
            base = url.rsplit("/", 1)[0]
            for spec in re.findall(r"""from ['"](\./[^'"]+)['"]""", response.text):
                queue.append(f"{base}/{spec[2:]}")

        # The whole graph, not just the entry point.
        assert len(seen) >= 7, sorted(seen)


class TestExport:
    """The raw archive, for the retraining job.

    The job used to read ``/history``, which downsamples above
    TARGET_CHART_POINTS. On a 92-day archive that turned ~26,500 readings
    into 734 three-hourly averages, from which none of the nowcast's
    features can be built — so every run produced zero samples and exited
    green. These guard the endpoint that replaced it.
    """

    async def seed(self, client, count: int, start_minutes: int = 0):
        for i in range(count):
            await client.post("/api/weather", json=at(start_minutes + i * 5))

    async def test_requires_the_api_key_when_one_is_set(self, client, monkeypatch):
        from app.config import get_settings

        monkeypatch.setenv("API_KEY", "shh")
        get_settings.cache_clear()
        try:
            assert (await client.get("/api/weather/export")).status_code == 401
            response = await client.get(
                "/api/weather/export", headers={"X-API-Key": "shh"})
            assert response.status_code == 200
        finally:
            get_settings.cache_clear()

    async def test_returns_every_reading_raw(self, client):
        await self.seed(client, 40)
        body = (await client.get("/api/weather/export")).json()
        assert len(body["readings"]) == 40
        assert body["next"] is None

    async def test_readings_are_not_downsampled(self, client):
        """The bug this endpoint exists for: /history buckets, /export must not."""
        await self.seed(client, 60)
        export = (await client.get("/api/weather/export")).json()["readings"]
        assert len(export) == 60
        # Every point is a real stored reading, not an average with a band.
        for reading in export:
            assert set(reading) == {"timestamp", "utc_offset",
                                    "temperature", "humidity", "pressure"}
            assert "temperature_min" not in reading

    async def test_pages_through_with_the_cursor(self, client):
        await self.seed(client, 25)
        seen, cursor, pages = [], {}, 0
        while True:
            params = {"limit": 10, **cursor}
            body = (await client.get("/api/weather/export", params=params)).json()
            seen += body["readings"]
            pages += 1
            if not body["next"]:
                break
            cursor = body["next"]
            assert pages < 10, "cursor did not advance"
        assert len(seen) == 25
        assert len({r["timestamp"] for r in seen}) == 25
        assert [r["timestamp"] for r in seen] == sorted(r["timestamp"] for r in seen)

    async def test_the_cursor_is_exclusive(self, client):
        await self.seed(client, 5)
        first = (await client.get("/api/weather/export", params={"limit": 2})).json()
        second = (await client.get("/api/weather/export",
                                   params={"limit": 2, **first["next"]})).json()
        assert not ({r["timestamp"] for r in first["readings"]}
                    & {r["timestamp"] for r in second["readings"]})

    async def test_an_upper_bound_is_inclusive(self, client):
        await self.seed(client, 10)
        everything = (await client.get("/api/weather/export")).json()["readings"]
        cut = everything[4]["timestamp"]
        bounded = (await client.get("/api/weather/export", params={"to": cut})).json()
        assert [r["timestamp"] for r in bounded["readings"]] == \
            [r["timestamp"] for r in everything[:5]]

    async def test_half_a_cursor_is_a_bad_request(self, client):
        assert (await client.get(
            "/api/weather/export", params={"after": "2026-06-20 21:00:00"}
        )).status_code == 400

    async def test_an_unparseable_cursor_is_a_bad_request(self, client):
        assert (await client.get(
            "/api/weather/export", params={"after": "nope", "after_offset": 0}
        )).status_code == 400

    async def test_the_page_size_is_capped(self, client):
        from app import database

        over = database.EXPORT_MAX_PAGE_SIZE + 1
        assert (await client.get(
            "/api/weather/export", params={"limit": over})).status_code == 422

    async def test_both_passes_of_a_repeated_hour_survive_the_cursor(self, client):
        """A timestamp is not unique, so a timestamp-only cursor would skip one."""
        for offset in ("+02:00", "+01:00"):
            await client.post("/api/weather", json={
                **READING, "timestamp": f"2026-10-25T02:30:00{offset}"})

        first = (await client.get("/api/weather/export", params={"limit": 1})).json()
        assert first["readings"][0]["utc_offset"] == 120, "earlier instant first"
        second = (await client.get("/api/weather/export",
                                   params={"limit": 1, **first["next"]})).json()
        assert second["readings"][0]["utc_offset"] == 60


class TestInstallable:
    """Enough for a phone to keep this on a home screen."""

    async def test_the_manifest_describes_the_app(self, client):
        body = (await client.get("/manifest.webmanifest")).json()
        assert body["name"] == "Balcony Weather Station"
        assert body["start_url"] == "/"
        assert body["display"] == "standalone"

    async def test_the_manifest_speaks_the_readers_language(self, client):
        body = (await client.get(
            "/manifest.webmanifest", headers={"Accept-Language": "de-DE,de;q=0.9"}
        )).json()
        assert body["lang"] == "de"
        assert body["name"] == "Balkon-Wetterstation"

    async def test_it_varies_by_language(self, client):
        response = await client.get("/manifest.webmanifest")
        assert "Accept-Language" in [
            key.strip() for key in response.headers["vary"].split(",")
        ]

    async def test_every_icon_it_names_is_served(self, client):
        body = (await client.get("/manifest.webmanifest")).json()
        assert body["icons"], "the manifest needs icons to be installable"
        for icon in body["icons"]:
            assert (await client.get(icon["src"])).status_code == 200, icon["src"]

    async def test_the_page_links_the_manifest_and_a_touch_icon(self, client):
        markup = (await client.get("/")).text
        assert 'rel="manifest"' in markup
        assert 'rel="apple-touch-icon"' in markup

    async def test_the_touch_icon_exists(self, client):
        from app.config import get_settings

        response = await client.get(
            f"/static/{get_settings().asset_version}/icons/apple-touch-icon.png"
        )
        assert response.status_code == 200
        assert response.content[:8] == b"\x89PNG\r\n\x1a\n"

    async def test_theme_color_matches_the_stylesheet(self, client):
        """Two copies of a colour, in a template and a stylesheet, is exactly
        the sort of pair that drifts — a <meta> cannot read a CSS variable,
        so this checks them against each other instead."""
        from app.main import DARK_BACKGROUND, LIGHT_BACKGROUND

        css = (Path(__file__).resolve().parent.parent / "app" / "static"
               / "css" / "dashboard.css").read_text()
        declared = re.findall(r"--bg:\s*(#[0-9a-fA-F]{6})", css)
        assert declared[:2] == [LIGHT_BACKGROUND, DARK_BACKGROUND], declared

        markup = (await client.get("/")).text
        assert f'content="{LIGHT_BACKGROUND}"' in markup
        assert f'content="{DARK_BACKGROUND}"' in markup


class TestSecurityHeaders:
    """Sent on everything, and strict enough to be worth sending."""

    @pytest.mark.parametrize("path", [
        "/",
        "/api/weather/status",
        "/healthz",
    ])
    async def test_every_response_carries_them(self, client, path):
        await client.post("/api/weather", json=READING)
        headers = (await client.get(path)).headers
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "no-referrer"
        assert "default-src 'self'" in headers["content-security-policy"]

    async def test_static_assets_carry_them_too(self, client):
        from app.config import get_settings

        headers = (
            await client.get(f"/static/{get_settings().asset_version}/js/main.js")
        ).headers
        assert headers["x-content-type-options"] == "nosniff"

    async def test_the_policy_allows_no_inline_execution(self, client):
        """The template has no inline CSS or JS, and the charting library is
        served from here, so nothing needs an escape hatch. If one is added,
        it should be a deliberate act with this test in the diff."""
        policy = (await client.get("/")).headers["content-security-policy"]
        assert "unsafe-inline" not in policy
        assert "unsafe-eval" not in policy
        for directive in ("base-uri 'none'", "form-action 'none'",
                          "frame-ancestors 'none'", "object-src 'none'"):
            assert directive in policy, directive


class TestThePageIsSelfContained:
    """Nothing the browser loads may come from anywhere but this app.

    The forecast, the nowcast and every number on the page are computed from
    the station's own readings, and the app fetches nothing at runtime. Two
    CDN script tags were the exception, and the expensive kind: a script with
    no integrity hands whoever controls that origin the run of the page. If
    one reappears, this fails rather than the deployment quietly depending on
    a third party again.
    """

    async def test_no_external_resource_is_referenced(self, client):
        for minutes in range(0, 120, 5):
            await client.post("/api/weather", json=at(minutes))
        markup = (await client.get("/")).text

        external = re.findall(r'(?:src|href)="(https?://[^"]+)"', markup)
        assert external == [], external

    def test_the_charting_library_is_in_the_image(self):
        vendor = Path(__file__).resolve().parent.parent / "app" / "static" / "vendor"
        for name in ("chart.umd.js", "chartjs-adapter-date-fns.bundle.min.js"):
            asset = vendor / name
            assert asset.is_file(), name
            # Checked in as npm publishes it, licence banner and all.
            assert "MIT" in asset.read_text(errors="ignore")[:400], name

    async def test_the_vendored_library_is_actually_served(self, client):
        from app.config import get_settings

        version = get_settings().asset_version
        response = await client.get(f"/static/{version}/vendor/chart.umd.js")
        assert response.status_code == 200
        assert "Chart.js v4.4.0" in response.text[:400]


class TestTrainerReadsTheRawArchive:
    """ml/train.py must not go back to the endpoint that downsamples."""

    SOURCE = Path(__file__).resolve().parent.parent / "ml" / "train.py"

    def urls(self) -> set[str]:
        """String literals the trainer actually uses, docstrings excluded.

        The comments explain at length why /history is the wrong endpoint, so
        a plain substring search over the file would match the warning rather
        than a call.
        """
        import ast

        tree = ast.parse(self.SOURCE.read_text())
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        return {
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        }

    def test_it_fetches_the_export_endpoint(self):
        assert any("/api/weather/export" in u for u in self.urls())

    def test_it_does_not_fetch_the_history_endpoint(self):
        """/history buckets above TARGET_CHART_POINTS; the replay needs raw rows."""
        assert not [u for u in self.urls() if "/api/weather/history" in u]

    def test_it_sends_the_api_key(self):
        assert "X-API-Key" in self.SOURCE.read_text()

    def test_the_retrain_workflow_passes_the_key(self):
        workflow = (self.SOURCE.parent.parent
                    / ".github" / "workflows" / "retrain.yml").read_text()
        assert "API_KEY: ${{ secrets.API_KEY }}" in workflow


class TestTrainerAndAppAgree:
    """Constants the trainer bakes into labels and the app prints to readers.

    Parsed rather than imported, like the test above: ml/train.py pulls in
    numpy and scikit-learn, which are training dependencies and deliberately
    absent from the image. A disagreement here would be invisible — the page
    would describe one threshold while the model had been fitted to another.
    """

    SOURCE = Path(__file__).resolve().parent.parent / "ml" / "train.py"

    def assigned(self, name: str):
        import ast

        tree = ast.parse(self.SOURCE.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets
            ):
                return ast.literal_eval(node.value)
        raise AssertionError(f"{name} is not assigned in {self.SOURCE.name}")

    def test_overcast_means_the_same_on_both_sides(self):
        from app import nowcast

        assert self.assigned("OVERCAST_PERCENT") == nowcast.OVERCAST_PERCENT

    def test_the_horizon_is_the_same_on_both_sides(self):
        from app import nowcast

        assert self.assigned("HORIZON_HOURS") == nowcast.HORIZON_HOURS

    def test_the_rain_amount_is_the_same_on_both_sides(self):
        from app import nowcast

        assert self.assigned("RAIN_MM") == nowcast.RAIN_MM

    def test_the_trainer_fits_both_targets_from_one_feature_tuple(self):
        """Two models, one vector: a feature that meant different things to
        the two would make their contributions incomparable."""
        features = self.assigned("FEATURES")
        from app import nowcast

        assert set(features) <= set(nowcast.FEATURE_FORMATS)

    def test_the_trainer_writes_the_path_the_app_loads(self):
        from app import nowcast

        assert nowcast.SKY_MODEL_PATH.name in self.SOURCE.read_text()
        assert nowcast.MODEL_PATH.name in self.SOURCE.read_text()
