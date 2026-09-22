"""Application entry point: wiring, lifespan and the dashboard route."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, backup, database, i18n, services, weather
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

#: The page background in each theme, from dashboard.css. Sent as
#: theme-color so the browser chrome and an installed window match the page
#: rather than framing it in grey. Duplicated from the stylesheet because a
#: <meta> cannot read a CSS variable; the tests assert the two agree.
LIGHT_BACKGROUND = "#f0f4f8"
DARK_BACKGROUND = "#1a202c"

#: A year, the longest max-age HTTP defines a meaning for.
ASSET_MAX_AGE_SECONDS = 31_536_000

#: Sent on every response.
#:
#: The policy can be this strict because the page genuinely loads nothing
#: from anywhere else: the stylesheet, the ES modules and the charting
#: library are all served from here (see static/vendor/README.md). There is
#: no 'unsafe-inline' because the template has no inline CSS or JS — the two
#: inline <script> blocks are ``type="application/json"`` data, which the
#: browser never executes and CSP therefore does not govern.
#:
#: ``'none'`` for the rest says what this page is: it has no forms, embeds
#: nothing, frames nothing and is not meant to be framed.
CONTENT_SECURITY_POLICY = "; ".join((
    "default-src 'self'",
    # The one relaxation, and a narrow one: the favicon showing the current
    # temperature is drawn on a canvas and handed over as a data: URL, since
    # sixty possible degrees would otherwise be sixty files fetched over the
    # network for something the page already knows. data: is inert for
    # images — it cannot execute — unlike in script-src, which stays 'self'.
    "img-src 'self' data:",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
    "object-src 'none'",
))

SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    # The API returns JSON and the static mount serves CSS and JS. A browser
    # that sniffs its way to a different conclusion about any of them is
    # guessing, and the guess is the attack.
    "X-Content-Type-Options": "nosniff",
    # There is nothing to tell anyone. The page links nowhere off-site, so a
    # referrer could only leak which station someone is reading.
    "Referrer-Policy": "no-referrer",
}


class VersionedStaticFiles(StaticFiles):
    """Static files whose URL already names the build they came from.

    Versioning by path was done so that a deploy could not serve a stale
    module; the other half of that bargain is never asking about a file
    whose URL says exactly which bytes it is. Without a Cache-Control the
    browser heuristically caches and then revalidates, so every asset cost
    a conditional round trip on every single page load — for a file that
    cannot change, because a changed file has a different path.

    ``immutable`` says so explicitly: don't revalidate, not even on reload.
    Only the versioned mount gets this. The plain ``/static`` mount serves
    the same files at a path that does *not* name a build, and must keep
    revalidating.

    Nor does the promise hold when nothing set GIT_SHA or ASSET_VERSION: the
    path is then ``/static/2.0.0/…``, which stands still across releases, and
    a year of immutable would pin a browser to whatever it saw first. That is
    the original bug, so outside a built image this simply does not claim it.
    """

    def __init__(self, *args, immutable: bool, **kwargs):
        super().__init__(*args, **kwargs)
        self.immutable = immutable

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        if self.immutable:
            response.headers["Cache-Control"] = (
                f"public, max-age={ASSET_MAX_AGE_SECONDS}, immutable"
            )
        return response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    log.info("starting weatherpage %s (data_dir=%s, tz=%s, auth=%s)",
             __version__, settings.data_dir, settings.timezone.key,
             "on" if settings.requires_api_key else "off")
    await database.connect()
    # Started after the pool, since it borrows a connection from it, and
    # cancelled before the pool closes.
    snapshots = asyncio.create_task(backup.scheduler())
    try:
        yield
    finally:
        snapshots.cancel()
        with suppress(asyncio.CancelledError):
            await snapshots
        await database.disconnect()
        log.info("shut down cleanly")


def create_app() -> FastAPI:
    """Build the ASGI application."""
    application = FastAPI(
        title="Balcony Weather Station",
        version=__version__,
        lifespan=lifespan,
    )
    # Everything this app serves is text, and none of it was compressed. The
    # page is 31 KB in English and 52 KB in German — the extra 20 KB being the
    # string catalogue, which is shipped whole on purpose (see
    # i18n.page_payload) and is exactly the kind of repetitive JSON that
    # compresses to nothing. The 30-day chart series is 336 KB.
    #
    # Added here rather than left to the reverse proxy: whether nginx
    # compresses depends on its gzip_types, which lives in another
    # repository, and a page that is only small when someone else is
    # configured correctly is not small. A proxy that also compresses will
    # not compress this twice — it forwards a response that already carries
    # Content-Encoding.
    application.add_middleware(GZipMiddleware, minimum_size=1000)

    @application.middleware("http")
    async def security_headers(request: Request, call_next):
        """Attach :data:`SECURITY_HEADERS` to everything this app serves.

        Applied here rather than in the reverse proxy for the same reason
        compression is: that configuration lives in another repository, and
        a header the app depends on should travel with the app.
        """
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response

    application.include_router(api_router)

    # Assets are versioned by the path, not by a ?v= query, because the page
    # loads ES modules. A query string only versions the URL the browser is
    # given — main.js — while the `./heatmap.js` it imports resolves relative
    # to that URL and comes out unversioned, so the browser keeps serving
    # whatever it cached the first time. A page whose markup is new and whose
    # modules are months old renders wrong rather than stale, which is exactly
    # what happened. Relative imports inherit the directory, so versioning the
    # directory versions the whole graph, with no build step.
    #
    # The plain mount stays for anything holding an old link; the page itself
    # is no-store, so it always hands out the current prefix.
    application.mount(
        f"/static/{get_settings().asset_version}",
        VersionedStaticFiles(
            directory=str(STATIC_DIR),
            immutable=get_settings().asset_version_names_a_build,
        ),
        name="static-versioned",
    )
    application.mount(
        "/static", StaticFiles(directory=str(STATIC_DIR)), name="static"
    )

    @application.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest(request: Request) -> JSONResponse:
        """Enough for the page to be added to a phone's home screen.

        A balcony weather page is a page you check, not one you search for,
        so it is worth being one tap away. Served from a route rather than
        as a static file so the name is in the reader's language, like
        everything else here.
        """
        lang = i18n.negotiate(
            request.headers.get("accept-language"),
            request.query_params.get("lang"),
            request.cookies.get(i18n.LANGUAGE_COOKIE),
        )
        t = i18n.translator(lang)
        version = get_settings().asset_version
        response = JSONResponse({
            "name": t("Balcony Weather Station"),
            "short_name": t("Balcony Weather"),
            "description": t(
                "Live temperature, humidity and pressure from a balcony "
                "weather station."
            ),
            "lang": lang,
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            # The light theme's own values, so the window chrome matches the
            # page instead of framing it in browser grey.
            "background_color": LIGHT_BACKGROUND,
            "theme_color": LIGHT_BACKGROUND,
            "icons": [
                {"src": f"/static/{version}/icons/icon-192.png",
                 "sizes": "192x192", "type": "image/png"},
                {"src": f"/static/{version}/icons/icon-512.png",
                 "sizes": "512x512", "type": "image/png"},
                {"src": f"/static/{version}/icons/favicon.svg",
                 "sizes": "any", "type": "image/svg+xml"},
            ],
        })
        response.headers["Vary"] = "Accept-Language"
        return response

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
        explicit override — the switch in the header is a link to exactly
        that. The same choice is handed to the browser in ``i18n`` so the
        poller rewrites the elements in the language they were rendered in.

        An explicit choice is remembered in a cookie, because a reader whose
        browser asks for the other language would otherwise have to say so on
        every visit. Only an explicit one: the negotiated language is never
        written back, so the cookie always records something a person did
        rather than something this code guessed, which is also what keeps it
        a preference rather than something to ask consent for.
        """
        chosen = request.query_params.get("lang")
        lang = i18n.negotiate(
            request.headers.get("accept-language"),
            chosen,
            request.cookies.get(i18n.LANGUAGE_COOKIE),
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
                "rule": i18n.rule_describer(lang),
                # The explainer prints whichever ladder actually produced the
                # phrase on the banner. They are different documents: one is
                # thresholds on raw readings, the other bands on two fitted
                # probabilities, and showing the wrong one would describe
                # reasoning the page did not do.
                "ladder": (
                    weather.learned_ladder(
                        context["nowcast"]["threshold"],
                        sky=bool(context.get("sky_is_learned")),
                    )
                    if context.get("outlook_is_learned")
                    else weather.RULE_LADDER
                ),
                "outlook_is_learned": bool(context.get("outlook_is_learned")),
                "sky_is_learned": bool(context.get("sky_is_learned")),
                "calibration": weather.CALIBRATION,
                # Static between deploys — written weekly by ml/train.py —
                # so the render carries it and the poller leaves it alone.
                "verification": services.VERIFICATION,
                "percentile_days": database.PERCENTILE_DAYS,
                "smoothing_minutes": database.SMOOTHING_WINDOW_MINUTES,
                "date": i18n.date_formatter(lang),
                # The "last updated" line, in the two forms the template
                # needs: the words, and the timestamp behind them.
                "relative_age": (
                    i18n.format_relative(context["age_seconds"], lang)
                    if context.get("age_seconds") is not None
                    else None
                ),
                "full_timestamp": (
                    i18n.format_datetime(context["current"]["timestamp"], lang)
                    if context.get("current")
                    else ""
                ),
                "months": i18n.MONTHS_SHORT[lang],
                "i18n_payload": i18n.page_payload(lang),
                "asset_version": get_settings().asset_version,
                "light_background": LIGHT_BACKGROUND,
                "dark_background": DARK_BACKGROUND,
                "stale_after_minutes": STALE_AFTER_MINUTES,
            },
        )
        # The page embeds live readings; never let a proxy hold on to it.
        response.headers["Cache-Control"] = "no-store"
        # Belt and braces next to no-store: the markup varies by language,
        # and the cookie is now one of the things that decides which.
        response.headers["Vary"] = "Accept-Language, Cookie"
        if i18n.chosen_language(chosen) is not None:
            response.set_cookie(
                i18n.LANGUAGE_COOKIE,
                lang,
                max_age=i18n.LANGUAGE_COOKIE_MAX_AGE,
                path="/",
                httponly=True,
                samesite="lax",
            )
        return response

    return application


app = create_app()
