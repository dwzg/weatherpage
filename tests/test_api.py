"""End-to-end tests through the ASGI app."""

import re
from datetime import timedelta

import pytest

from app import clock

READING = {"temperature": "21.5", "humidity": "55", "pressure": "1013",
           "timestamp": "2026-06-20T21:00:00"}


def at(minutes_ago: int, **overrides) -> dict:
    moment = clock.now().replace(second=0, microsecond=0) - timedelta(minutes=minutes_ago)
    return {**READING, "timestamp": moment.strftime("%Y-%m-%dT%H:%M:00"), **overrides}


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
        assert set(body) == {"readings", "interval_seconds", "bucketed"}
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
