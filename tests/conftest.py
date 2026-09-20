"""Shared fixtures: an isolated database and a client wired to it."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point every test at its own data directory and a fixed timezone."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TIMEZONE", "Europe/Berlin")
    monkeypatch.delenv("API_KEY", raising=False)

    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def db(_isolated_settings):
    """An open, empty database."""
    from app import cache, database

    await database.connect()
    cache.invalidate()
    try:
        yield database
    finally:
        await database.disconnect()


@pytest.fixture
def make_readings():
    """Build a run of readings on the 5-minute grid, ending at ``end``."""

    def _make(count: int, end: datetime, step_minutes: int = 5, **overrides):
        readings = []
        for i in range(count):
            moment = end - timedelta(minutes=step_minutes * (count - 1 - i))
            readings.append({
                "temperature": overrides.get("temperature", 20.0),
                "humidity": overrides.get("humidity", 55.0),
                "pressure": overrides.get("pressure", 1013.0),
                "timestamp": moment.strftime("%Y-%m-%d %H:%M:%S"),
            })
        return readings

    return _make


@pytest_asyncio.fixture
async def client(_isolated_settings):
    """An httpx client running the app through its real lifespan."""
    import httpx

    from app.main import create_app

    application = create_app()
    transport = httpx.ASGITransport(app=application)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
        application.router.lifespan_context(application),
    ):
        yield c
