# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Dynamic weather dashboard ("Balcony Weather Station") served by a FastAPI app in a single Docker container. Weather data comes from a Home Assistant sensor every 5 minutes, currently relayed through GitHub Actions. The app stores all readings in SQLite and provides both a JSON API and a browser dashboard with Chart.js history graphs.

## Architecture

```
 Home Assistant → GH Actions (relay) → Docker App (FastAPI + SQLite)
                                            ├── POST /api/weather     ← data ingestion
                                            ├── GET  /api/weather/current
                                            ├── GET  /api/weather/history?period=...
                                            ├── GET  /api/weather/stats?period=...
                                            └── GET  /                 ← serves UI
```

- **`app/main.py`** — FastAPI application with API endpoints and UI route
- **`app/database.py`** — SQLite layer (async via `aiosqlite`): init, insert, query, stats
- **`app/templates/index.html`** — Jinja2 template: current conditions, Chart.js history graphs, period selector, stats cards
- **`Dockerfile`** — Python 3.12 slim, runs uvicorn on port 8080. SQLite DB persisted via `/data` volume
- **`.github/workflows/deploy.yml`** — Triggered on push to `main`. Builds Docker image, pushes to GHCR, triggers Portainer webhook to redeploy.
- **`.github/workflows/main.yml`** — `workflow_dispatch` relay: receives weather inputs from Home Assistant and POSTs them to the running app.

## Deployment

The app is deployed via Docker Compose managed by Portainer. The compose file lives in a separate repo at `~/git/docker-compose/weatherpage/docker-compose.yml`:
- Image: `ghcr.io/dwzg/weatherpage:latest`
- Volume: `/opt/docker/weatherpage:/data` (persists SQLite DB)
- Network: external `nginx-proxy-network`

On every push to `main`, the `deploy.yml` workflow builds and pushes a new image to GHCR, then triggers the Portainer webhook which pulls the new image and restarts the container.

## Local Development

```bash
# Install dependencies
pip install -r app/requirements.txt

# Run the app
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload

# The SQLite DB is created at /data/weather.db — for local dev, set DATA_DIR:
mkdir -p /tmp/weather_data && DATA_DIR=/tmp/weather_data uvicorn app.main:app --port 8080
```

## Testing with Docker

```bash
docker build -t weatherpage .
docker run -p 8080:8080 -v weather_data:/data weatherpage

# Post test data
curl -X POST http://localhost:8080/api/weather \
  -H 'Content-Type: application/json' \
  -d '{"temperature":"22.5","humidity":"55","pressure":"1013","timestamp":"2026-06-20T21:00:00"}'

# View dashboard at http://localhost:8080
```

## GitHub Secrets

- `APP_URL` — base URL of the running app. Used by the relay workflow (`main.yml`) to forward HA webhook data.
- `PORTAINER_WEBHOOK_URL` — Portainer webhook URL triggered by `deploy.yml` after a successful image push.
- `GITHUB_TOKEN` — auto-provided, used for GHCR login and push.
- `DELETE_PACKAGES_TOKEN` — personal access token with `delete:packages` scope for cleaning old untagged images.

## Future: Direct Home Assistant Integration

When ready, change Home Assistant to POST directly to `https://<app-url>/api/weather`. The GitHub Actions relay (`main.yml`) can then be removed.
