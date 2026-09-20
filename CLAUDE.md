# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Dynamic weather dashboard ("Balcony Weather Station") served by a FastAPI app in a single Docker container. Weather data comes from a Home Assistant sensor every 5 minutes, currently relayed through GitHub Actions. The app stores all readings in SQLite and provides both a JSON API and a browser dashboard with Chart.js history graphs.

## Architecture

```
 Home Assistant → GH Actions (relay) → Docker App (FastAPI + SQLite)
                                            ├── POST   /api/weather            ← data ingestion (API key)
                                            ├── DELETE /api/weather/cleanup    ← off-grid/range pruning (API key)
                                            ├── GET    /api/weather/current
                                            ├── GET    /api/weather/status     ← everything the dashboard polls
                                            ├── GET    /api/weather/history?period=...
                                            ├── GET    /api/weather/stats?period=...
                                            ├── GET    /api/weather/daily?months=...  ← calendar heatmap
                                            ├── GET    /healthz                ← container healthcheck
                                            └── GET    /                       ← serves UI
```

### Modules

| File | Responsibility |
| --- | --- |
| `app/config.py` | Every environment variable, read once into a frozen `Settings`. Nothing else touches `os.environ`. |
| `app/clock.py` | The timestamp convention: formatting, parsing, period cutoffs, month arithmetic. |
| `app/weather.py` | Pure derived values — dew point, heat index, the forecast rules engine, forecast emoji. No I/O. |
| `app/nowcast.py` | Loads `app/model.json` and evaluates it. Pure arithmetic — no ML dependency in the image. |
| `app/model.json` | The fitted nowcast, written by CI. Data, not code: treat it as something that might be wrong. |
| `ml/train.py` | The retraining job. Runs in CI only; the one thing in this project that fetches anything. |
| `app/database.py` | All SQLite access: connection pool, migrations, queries. |
| `app/cache.py` | Memoisation for the whole-table aggregates, dropped on every write. |
| `app/models.py` | Pydantic request/response schemas, including the sensor plausibility ranges. |
| `app/services.py` | Assembles the dashboard payload shared by the API and the page render. |
| `app/api.py` | The `/api/weather` router and the API-key dependency. |
| `app/main.py` | App factory, lifespan, the `/` and `/healthz` routes, static mount. |
| `app/templates/index.html` | Markup only — no inline CSS or JS. |
| `app/static/css/dashboard.css` | All styles. |
| `app/static/js/*.js` | ES modules: `format` (shared helpers), `charts`, `heatmap`, `climate`, `poll`, `main` (entry point). |
| `backfill.py` | Standalone script (not in the image) that replays a Home Assistant window to fill an outage. |

## Data conventions

These are implicit across the codebase and easy to break:

- **Timestamps are naive local-time strings**, `YYYY-MM-DD HH:MM:SS`, matching what Home Assistant sends. Nothing is stored in UTC and no rows carry an offset.
- **Range queries are lexicographic string comparisons.** Build every bound through `clock.fmt_ts()` / `clock.period_cutoff()` — comparing against an ISO-with-`T` or UTC string silently matches nothing. `clock.normalise_ts()` is the only entry point for incoming timestamps.
- Because the format is fixed-width, the aggregate queries slice it with `substr()` instead of `strftime()`/`date()`. Same results, measurably faster over a large archive.
- The timezone comes from the `TIMEZONE` env var (default `Europe/Berlin`) via `clock.now()`. Never use a bare `datetime.now()`.
- **`timestamp` is UNIQUE.** Ingestion upserts, so re-posting a slot corrects it rather than duplicating it. On first start against an older database the migration de-duplicates (keeping the earliest row per timestamp) and then adds the index.
- **Readings are expected on a 5-minute grid with `:00` seconds.** `remove_off_grid_readings()` deletes anything off-grid; `remove_readings_in_range()` clears a window. Chart gaps are a real signal, so don't "fix" missing slots by interpolating.
- **Incoming readings are range-checked** (`app/models.py`): temperature −90…60 °C, humidity 0…100 %, pressure 800…1100 hPa. A sensor glitch or a Home Assistant `unavailable` is rejected with 422 rather than stored forever.
- **The forecast reads pressure as a level, not a tendency.** Scored against observed hourly rainfall at Rheinfelden over 90 days, the barometric tendency has *negative* skill at this station (a 6h fall of >1 hPa scored KSS −0.09; inside the wettest conditions rising pressure was followed by rain more often than falling). `get_pressure_percentile()` ranks the current reading against the station's own last 30 days, because a fixed hPa threshold does not transfer between months (CSI 0.05 in one, 0.44 in another). That rank plus humidity is the whole forecast; the tendency is still measured and shown on the pressure card, but nothing predictive is built on it. Don't reintroduce "falling barometer means rain" — it was measured and it is wrong here.
- **The pressure trend is de-tided**, and the percentile depends on that too.- **Both ends of every trend are medians** over `SMOOTHING_WINDOW_MINUTES`, not single readings, so one noisy sample cannot push a delta across a threshold.
- **Ordering is by `timestamp`, never by `id`.** A backfill inserts old readings with fresh ids, so `ORDER BY id DESC` would make a backfilled row "current".

## Database access

- A small connection pool (`database.POOL_SIZE`) is opened in the lifespan via `connect()` and closed by `disconnect()`. Use `async with database.acquire() as db:` — don't open your own connection.
- **Migrations run before the pool is opened.** A connection caches the schema it saw at open time, and `ON CONFLICT(timestamp)` is resolved when a statement is prepared, so a connection opened before the unique index existed would reject every upsert for the life of the process.
- The whole-table aggregates (`get_stats`, `get_extremes_with_times`, `get_climate_stats`, `get_daily_extremes`, `get_daily_summaries`, `get_history_series`) are `@cached`. Any new write path must call `invalidate_cache()`, or the dashboard will serve stale numbers until the next reading arrives.
- `get_history_series()` downsamples: a period whose raw series would exceed `TARGET_CHART_POINTS` is averaged into buckets, and each point then carries `*_min`/`*_max` for the range band. `/api/weather/history` returns `{readings, interval_seconds, bucketed}` — not a bare array.

## The two predictions

The dashboard shows two, deliberately different in kind, and the page itself
explains the difference to the reader under "How these two predictions work".

| | rule-based outlook | learned nowcast |
| --- | --- | --- |
| lives in | `app/weather.py` | `app/nowcast.py` + `app/model.json` |
| output | a phrase (`Rain likely`) | a probability (`38%`) |
| fitted by | hand, from measured tiers | `ml/train.py`, weekly in CI |
| answers | is it settling or deteriorating | chance of ≥0.2 mm within 6 h |

Neither may fetch anything at runtime, and neither needs to.

### How the nowcast is trained and shipped

`ml/train.py` (run by `.github/workflows/retrain.yml`, Mondays) pulls the
station's readings from its own public history endpoint and observed hourly
rainfall from Open-Meteo's ERA5 archive for the labels. It fits a logistic
regression, scores it walk-forward with weekly refits, and writes
`app/model.json` **only if** the candidate clears the gates in that file:
skill over climatology, ranking at least as well as the rules, and no sharp
regression against the shipped model. Refusing to ship is a normal outcome.

Three things are load-bearing here:

- **One feature path.** Training does not reimplement the features: it calls
  `services.nowcast_features()`, the same function the running app calls,
  against a throwaway database with the clock pinned to each historical hour.
  Add a feature by adding it there and to `FEATURES` in `ml/train.py`, never
  by computing it separately in the trainer.
- **Readings are fed in as the replay clock reaches them.** The app's
  "latest reading" queries are `ORDER BY timestamp DESC LIMIT n` with no upper
  bound — right in production, but against a fully populated table every
  historical hour would get the values from the end of the series. That bug
  produced a model that looked plausible and had learned nothing.
- **The image carries no ML dependency.** `app/nowcast.py` evaluates the
  model with a dot product and a sigmoid; numpy and scikit-learn live in
  `ml/requirements.txt` and are installed only by the retraining job.

A `GITHUB_TOKEN` push does not start another workflow, so the retraining job
cannot deploy by committing — it calls `deploy.yml` through
`workflow_dispatch` explicitly. Remove that trigger and retrained models will
sit on `main` undeployed.

### Calibrating the forecast

The rules in `app/weather.py` were fitted offline against observed hourly precipitation for Rheinfelden (Open-Meteo, ERA5 archive plus the 2 km ICON series as an independent check), joined to the station's own readings. **The app fetches nothing at runtime and must stay that way** — the page is self-contained by design. If you retune, score phrases by their observed rain-within-6h frequency rather than by eye: the current tiers run 69% / 44% / 32% / 28% down to 4% against a 21% base rate, and the binary "says rain" forecast scores CSI 0.27 (KSS +0.27), where the rules it replaced scored KSS −0.074 — worse than saying nothing.

## Frontend conventions

- No build step, no framework, no bundler. The page loads `static/js/main.js` as an ES module; Chart.js 4 and its date-fns adapter come from jsDelivr, so **charts need network access** — without it the page still renders values, records and the calendar, and shows a note where the charts would be.
- **Asset URLs are root-relative (`/static/...`), deliberately.** `url_for()` builds an absolute URL from the request, which behind the HTTPS reverse proxy comes out as `http://` and is blocked as mixed content, leaving the page with no CSS and no JS.
- The page is server-rendered, then `poll.js` updates the same elements every 60 s. Anything the server renders *and* the poller rewrites must have one source of truth: the forecast emoji is computed server-side and sent in `/status`, and every year's Monthly Details table is rendered server-side with the year switcher only toggling `hidden`.
- Charts use a **time scale**; `toTimeData()` inserts a `NaN` point when consecutive points are more than three intervals apart, so outages show as breaks of proportional width. The threshold comes from the response's `interval_seconds`, so a bucketed series doesn't read as one long outage.
- Axis ticks are labelled by `tickFormatter`, which picks decimals from the tick step — pressure spans ~2 hPa a day and would otherwise repeat the same whole number.
- Sparklines are `responsive: true` inside a fixed-size `.sparkline-wrap`. Sizing the canvas directly does not work: Chart.js writes an inline width onto it, which overrides the stylesheet and stops the card shrinking.
- **Grid overflow gotcha:** grid items default to `min-width: auto`, so a card whose content has a wide minimum pushes the grid past the viewport. `.card` sets `min-width: 0`.
- Card spacing: `.stats-card` has no bottom margin because inside `.stats-grid` the grid `gap` does the spacing; `.container > .stats-card` adds its own.

## Local Development

```bash
pip install -r requirements-dev.txt

mkdir -p /tmp/weather_data
DATA_DIR=/tmp/weather_data uvicorn app.main:app --reload --port 8080
```

Templates and static files are resolved from the package directory, so the working directory no longer matters.

An empty database renders the "waiting for first reading" placeholder, so seed some rows before working on the UI. Several sections only appear with enough history: the climate chart and Monthly Details need complete days, and the calendar needs daily summaries.

## Testing

`pytest` and `ruff` run on every pull request (`.github/workflows/ci.yml`), alongside a Docker build and a container smoke test.

```bash
pytest -q          # 191 tests
ruff check .
```

`tests/conftest.py` gives each test its own `DATA_DIR`, a fixed timezone and a clean settings cache. Use the `db` fixture for the storage layer and `client` for anything that goes through HTTP.

For UI changes, drive the page in a real browser (Playwright is available). Measuring the DOM — element boxes, computed styles, console errors — is more reliable than eyeballing a screenshot for spacing work. The sandbox cannot reach jsDelivr, so intercept those requests and serve Chart.js from a local copy if you need the charts to render.

```bash
curl -X POST http://localhost:8080/api/weather \
  -H 'Content-Type: application/json' \
  -d '{"temperature":"22.5","humidity":"55","pressure":"1013","timestamp":"2026-06-20T21:00:00"}'

curl 'http://localhost:8080/api/weather/status'
curl 'http://localhost:8080/api/weather/history?period=all'
```

## Docker

```bash
docker build -t weatherpage .
docker run -p 8080:8080 -v weather_data:/data weatherpage
```

The image runs as root because the deployment bind-mounts a host directory to `/data`; the Dockerfile notes what to change to run unprivileged.

## Deployment

Deployed via Docker Compose managed by Portainer. The compose file lives in a separate repo at `~/git/docker-compose/weatherpage/docker-compose.yml`:
- Image: `ghcr.io/dwzg/weatherpage:latest`
- Volume: `/opt/docker/weatherpage:/data` (persists SQLite DB)
- Network: external `nginx-proxy-network`

Every push to `main` builds and pushes a new image, then triggers the Portainer webhook to pull and restart. Images are also tagged with the commit SHA, so a bad deploy can be rolled back by pinning the previous SHA in the compose file. Since the deploy only runs post-merge, a failure lands on `main` with nothing on the PR to show it: the webhook step is skipped and the old image keeps serving. The GHCR push occasionally fails with `ERROR: unknown blob`, which is a transient registry error — re-run the failed job.

## GitHub Secrets

- `APP_URL` — base URL of the running app. Used by the relay workflow (`main.yml`) to forward HA webhook data.
- `API_KEY` — shared secret protecting `POST /api/weather` and `DELETE /api/weather/cleanup`. Must match between the app (env var), the relay workflow, and (eventually) Home Assistant. When the env var is unset those endpoints are unauthenticated, which is how local dev works.
- `PORTAINER_WEBHOOK_URL` — Portainer webhook URL triggered by `deploy.yml` after a successful image push.
- `GITHUB_TOKEN` — auto-provided, used for GHCR login and push.
- `DELETE_PACKAGES_TOKEN` — personal access token with `delete:packages` scope for cleaning old untagged images.

Workflow inputs are passed to the shell through `env:`, never interpolated into a `run:` body — `${{ inputs.x }}` is substituted before the shell sees it, so a crafted value would execute as shell on the runner.

## Known limitations

- During the autumn DST fallback the local hour repeats, so two distinct instants share a timestamp and sort as equal. Fixing that means migrating the stored format.
- The aggregates scan the whole table; the cache hides this between writes, but a much longer archive would want a rollup table.

## Future: Direct Home Assistant Integration

When ready, change Home Assistant to POST directly to `https://<app-url>/api/weather`. The GitHub Actions relay (`main.yml`) can then be removed.
