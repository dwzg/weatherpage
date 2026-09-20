# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Dynamic weather dashboard ("Balcony Weather Station") served by a FastAPI app in a single Docker container. Weather data comes from a Home Assistant sensor every 5 minutes, currently relayed through GitHub Actions. The app stores all readings in SQLite and provides both a JSON API and a browser dashboard with Chart.js history graphs.

## Architecture

```
 Home Assistant → GH Actions (relay) → Docker App (FastAPI + SQLite)
                                            ├── POST   /api/weather            ← data ingestion (API key)
                                            ├── DELETE /api/weather/cleanup    ← off-grid/dupe pruning (API key)
                                            ├── GET    /api/weather/current
                                            ├── GET    /api/weather/status     ← everything the dashboard polls
                                            ├── GET    /api/weather/history?period=...
                                            ├── GET    /api/weather/stats?period=...
                                            ├── GET    /api/weather/daily?months=...  ← calendar heatmap
                                            └── GET    /                       ← serves UI
```

- **`app/main.py`** — FastAPI app. Holds the derived-value logic that is *not* in the database layer: `compute_dew_point`, `compute_heat_index` (NOAA Rothfusz, returns `None` below 27°C), and `compute_forecast`, a rules engine combining pressure trend/acceleration/consistency, humidity, temperature trend, dew point spread and time of year into a short forecast string.
- **`app/database.py`** — all SQLite access (async via `aiosqlite`). Single `weather_readings` table; everything else is an aggregate query (stats, extremes-with-times, daily summaries, climate stats, pressure/recent trends).
- **`app/templates/index.html`** — the entire frontend: one Jinja2 template with all CSS and JS inline (~1300 lines). Server-renders the initial page, then the JS takes over.
- **`backfill.py`** — standalone script (repo root, not in the image) that replays a Home Assistant history window into the app to fill an outage. Its `START`/`END` constants are edited per use; run as `source .env && python3 backfill.py`. Needs `HA_TOKEN` and `WP_API_KEY`, and posts to the production URL.
- **`Dockerfile`** — Python 3.12 slim, uvicorn on 8080, SQLite persisted via the `/data` volume. Copies only `app/`.
- **`.github/workflows/deploy.yml`** — on push to `main`: build image → push to GHCR → trigger Portainer webhook → prune untagged images.
- **`.github/workflows/main.yml`** — `workflow_dispatch` relay: receives weather inputs from Home Assistant and POSTs them to the running app.

## Data conventions

These are implicit across the codebase and easy to break:

- **Timestamps are naive local-time strings**, `YYYY-MM-DD HH:MM:SS`, matching what Home Assistant sends. `insert_reading` normalizes `T` → space. Nothing is stored in UTC and no rows carry an offset.
- **Range queries are lexicographic string comparisons.** `_period_to_cutoff()` produces a local-time `datetime`, `_fmt_cutoff()` formats it to the same string shape, and SQL compares `timestamp >= ?`. Keep any new query on that path — comparing against an ISO-with-`T` or UTC string silently matches nothing.
- The timezone comes from the `TIMEZONE` env var (default `Europe/Berlin`) via `database._now()`. Use that, not `datetime.now()`.
- **Readings are expected on a 5-minute grid with `:00` seconds.** `remove_off_grid_readings()` deletes anything off-grid plus duplicate timestamps (keeping the lowest id); `remove_readings_in_range()` clears a window and re-deduplicates. Chart gaps are a real signal, so don't "fix" missing slots by interpolating.
- **Every DB function opens and closes its own connection** (`db = await get_db()` … `finally: await db.close()`). There's no shared pool or app-level connection; follow the pattern rather than introducing one.
- `API_KEY` (env) guards only `POST /api/weather` and `DELETE /api/weather/cleanup`, via the `X-API-Key` header. When the env var is unset, those endpoints are unauthenticated — which is how local dev works.

## Frontend conventions

- No build step, no framework, no bundler. Chart.js 4 and its date-fns adapter load from jsDelivr, so **the page needs network access to render charts** — in a sandbox they fail with `Chart is not defined` while the rest of the page still works.
- `pollStatus()` hits `/api/weather/status` every 60s and updates the current cards, sparklines and (when the 24h period is active) the main charts in place. It never rebuilds the calendar or the climate chart, which are one-shot on load.
- Period buttons call `loadPeriod()`, which caches each period's response in `historyCache`.
- Charts use a **time scale**, and `toTimeData()` inserts a `NaN` point when consecutive readings are more than `GAP_THRESHOLD_MS` (15 min) apart, so outages show as breaks of proportional width rather than straight lines.
- The temperature calendar is a month pager: one panel per month in a horizontally scroll-snapping track, with `syncHeatmapHeight()` driving the track's height from the scroll offset so it fits the month in view.
- **Card spacing gotcha:** `.stats-card` intentionally has no bottom margin because inside `.stats-grid` the grid `gap` does the spacing. A stats-card placed directly in `.container` needs `.container > .stats-card`'s margin — an inline `grid-column: 1 / -1` on such a card is a no-op leftover.

## Local Development

```bash
pip install -r app/requirements.txt

# DATA_DIR must be set or the app tries to write to /data
mkdir -p /tmp/weather_data
DATA_DIR=/tmp/weather_data uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

Run uvicorn **from the repo root**: the Jinja environment is built with `FileSystemLoader("app/templates")`, a path relative to the working directory.

An empty database renders the "waiting for first reading" placeholder, so seed some rows before working on the UI — either POST to `/api/weather` or insert straight into `weather.db`. Several sections only appear with enough history: the climate chart and Monthly Details card need complete days, and the calendar needs daily summaries.

## Testing

There is no test suite, linter config or CI on pull requests — `deploy.yml` only runs on push to `main`. Verification is manual: run the app locally against seeded data and check the endpoints and the rendered page.

```bash
curl -X POST http://localhost:8080/api/weather \
  -H 'Content-Type: application/json' \
  -d '{"temperature":"22.5","humidity":"55","pressure":"1013","timestamp":"2026-06-20T21:00:00"}'

curl 'http://localhost:8080/api/weather/status'
curl 'http://localhost:8080/api/weather/daily?months=24'
```

For UI changes, driving the page in a real browser (Playwright is available in this environment) catches layout and JS errors that reading the template does not. Measuring the DOM — element boxes, computed styles, console errors — is more reliable than eyeballing a screenshot for spacing work.

## Docker

```bash
docker build -t weatherpage .
docker run -p 8080:8080 -v weather_data:/data weatherpage
```

## Deployment

Deployed via Docker Compose managed by Portainer. The compose file lives in a separate repo at `~/git/docker-compose/weatherpage/docker-compose.yml`:
- Image: `ghcr.io/dwzg/weatherpage:latest`
- Volume: `/opt/docker/weatherpage:/data` (persists SQLite DB)
- Network: external `nginx-proxy-network`

Every push to `main` builds and pushes a new image, then triggers the Portainer webhook to pull and restart. Since the deploy only runs post-merge, a failure lands on `main` with nothing on the PR to show it: the webhook step is skipped and the old image keeps serving. The GHCR push occasionally fails with `ERROR: unknown blob`, which is a transient registry error — re-run the failed job.

## GitHub Secrets

- `APP_URL` — base URL of the running app. Used by the relay workflow (`main.yml`) to forward HA webhook data.
- `API_KEY` — shared secret protecting the `POST /api/weather` endpoint. Must match between the app (env var), the relay workflow, and (eventually) Home Assistant.
- `PORTAINER_WEBHOOK_URL` — Portainer webhook URL triggered by `deploy.yml` after a successful image push.
- `GITHUB_TOKEN` — auto-provided, used for GHCR login and push.
- `DELETE_PACKAGES_TOKEN` — personal access token with `delete:packages` scope for cleaning old untagged images.

## Future: Direct Home Assistant Integration

When ready, change Home Assistant to POST directly to `https://<app-url>/api/weather`. The GitHub Actions relay (`main.yml`) can then be removed.
