"""Application entry point: wiring, lifespan and the dashboard route."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, database, i18n, services
from .api import router as api_router
from .config import STALE_AFTER_MINUTES, get_settings

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

#: Resolved from this file rather than the working directory, so the app no
#: longer has to be started from the repository root.
PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    log.info("starting weatherpage %s (data_dir=%s, tz=%s, auth=%s)",
             __version__, settings.data_dir, settings.timezone.key,
             "on" if settings.requires_api_key else "off")
    await database.connect()
    try:
        yield
    finally:
        await database.disconnect()
        log.info("shut down cleanly")


def create_app() -> FastAPI:
    """Build the ASGI application."""
    application = FastAPI(
        title="Balcony Weather Station",
        version=__version__,
        lifespan=lifespan,
    )
    application.include_router(api_router)
    application.mount(
        "/static", StaticFiles(directory=str(STATIC_DIR)), name="static"
    )

    @application.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        """Liveness probe for the container runtime, and which build is serving.

        ``commit`` answers "did the deploy actually land" without having to
        infer it from behaviour — the version string moves rarely, so a
        container still running last week's image looks identical otherwise.
        """
        current = await database.get_current()
        return {
            "status": "ok",
            "version": __version__,
            "commit": get_settings().git_sha,
            "latest_reading": current["timestamp"] if current else None,
        }

    @application.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard(request: Request) -> HTMLResponse:
        """Server-render the dashboard; the page then polls for updates.

        The language comes from the browser's ``Accept-Language``, which is
        what a German-language OS sets, with ``?lang=en`` / ``?lang=de`` as an
        explicit override. The same choice is handed to the browser in
        ``i18n`` so the poller rewrites the elements in the language they
        were rendered in.
        """
        lang = i18n.negotiate(
            request.headers.get("accept-language"),
            request.query_params.get("lang"),
        )
        context = await services.build_page_context()
        response = templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                **context,
                "lang": lang,
                "t": i18n.translator(lang),
                "num": i18n.number_formatter(lang),
                "months": i18n.MONTHS_SHORT[lang],
                "i18n_payload": i18n.page_payload(lang),
                "asset_version": get_settings().asset_version,
                "stale_after_minutes": STALE_AFTER_MINUTES,
            },
        )
        # The page embeds live readings; never let a proxy hold on to it.
        response.headers["Cache-Control"] = "no-store"
        # Belt and braces next to no-store: the markup varies by language.
        response.headers["Vary"] = "Accept-Language"
        return response

    return application


app = create_app()
