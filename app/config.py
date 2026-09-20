"""Runtime configuration, read once from the environment.

Every tunable the app has lives here so the rest of the code never touches
``os.environ`` directly and tests can build a ``Settings`` by hand.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

#: Readings are expected on this grid (see the data conventions in CLAUDE.md).
READING_INTERVAL_MINUTES = 5

#: A reading older than this means the sensor (or the relay) has stopped.
STALE_AFTER_MINUTES = 15


@dataclass(frozen=True)
class Settings:
    """Immutable view of the process environment."""

    data_dir: Path
    timezone: ZoneInfo
    api_key: str | None
    #: Serialised into asset URLs so a deploy busts the browser cache.
    asset_version: str
    #: The commit this image was built from, baked in by the Dockerfile.
    #: "unknown" outside a built image, which is the honest answer locally.
    git_sha: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "weather.db"

    @property
    def requires_api_key(self) -> bool:
        """Ingestion endpoints are open when no key is configured (local dev)."""
        return bool(self.api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, built on first use."""
    from . import __version__

    return Settings(
        data_dir=Path(os.environ.get("DATA_DIR", "/data")),
        timezone=ZoneInfo(os.environ.get("TIMEZONE", "Europe/Berlin")),
        api_key=os.environ.get("API_KEY") or None,
        asset_version=os.environ.get("ASSET_VERSION") or __version__,
        git_sha=os.environ.get("GIT_SHA") or "unknown",
    )
