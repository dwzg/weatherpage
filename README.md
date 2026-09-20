# Balcony Weather Station

A small weather dashboard: a Home Assistant sensor posts temperature, humidity
and pressure every five minutes, a FastAPI app stores them in SQLite, and a
single page shows the current conditions, history charts, records and a
temperature calendar.

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)

## Quick start

```bash
pip install -r requirements-dev.txt

mkdir -p /tmp/weather_data
DATA_DIR=/tmp/weather_data uvicorn app.main:app --reload --port 8080
```

Then open <http://localhost:8080>. An empty database shows a placeholder, so
post a reading:

```bash
curl -X POST http://localhost:8080/api/weather \
  -H 'Content-Type: application/json' \
  -d '{"temperature":"22.5","humidity":"55","pressure":"1013","timestamp":"2026-06-20T21:00:00"}'
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATA_DIR` | `/data` | Directory holding `weather.db`. |
| `TIMEZONE` | `Europe/Berlin` | Local timezone; all timestamps are stored in it. |
| `API_KEY` | *(unset)* | Guards the two write endpoints. Unset means they are open — convenient locally, not in production. |
| `LOG_LEVEL` | `INFO` | Standard logging level. |
| `ASSET_VERSION` | app version | Appended to CSS/JS URLs to bust the browser cache. |

## API

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/api/weather` | Ingest one reading. Requires `X-API-Key` when configured. Upserts on `timestamp`. |
| `DELETE` | `/api/weather/cleanup` | Remove off-grid readings, or everything in `?from=…&to=…`. Requires `X-API-Key`. |
| `GET` | `/api/weather/current` | Newest reading. |
| `GET` | `/api/weather/status` | Everything the dashboard polls: current values, trends, forecast, staleness. |
| `GET` | `/api/weather/history?period=` | `3h`, `24h`, `7d`, `30d`, `today`, `all`. |
| `GET` | `/api/weather/stats?period=` | Min/max/avg per metric. |
| `GET` | `/api/weather/daily?months=` | Per-day summaries for the calendar (1–24 months). |
| `GET` | `/healthz` | Liveness probe; reports the newest reading. |

Interactive docs are at `/docs`.

### History responses are downsampled

`/api/weather/history` returns an object, not a bare array:

```json
{"readings": [...], "interval_seconds": 300, "bucketed": false}
```

Short periods come back raw. Longer ones are averaged into fixed-width buckets
so the whole archive is a few hundred kilobytes rather than tens of megabytes,
and each point then also carries `temperature_min` / `temperature_max` (and the
same for the other metrics) so the chart can shade the range the average hides.
`interval_seconds` is how far apart points are, which is what lets the chart
tell a bucket boundary from a real outage.

## Ingestion is validated

Readings are range-checked before they are stored, because the feed is
unattended and one bad value would skew every chart and all-time record
permanently:

| Metric | Accepted range |
| --- | --- |
| Temperature | −90 … 60 °C |
| Humidity | 0 … 100 % |
| Pressure | 800 … 1100 hPa |

Anything outside those, a non-numeric value such as Home Assistant's
`unavailable`, or an unparseable timestamp is rejected with `422`.

## Backfilling an outage

`backfill.py` replays a window of Home Assistant history into the app. It is
not part of the image; run it from a checkout with `HA_TOKEN` and `WP_API_KEY`
in the environment.

```bash
source .env
python3 backfill.py --start 2026-07-16T22:25 --end 2026-07-17T20:10 --dry-run
python3 backfill.py --start 2026-07-16T22:25 --end 2026-07-17T20:10
```

Re-running it is safe: ingestion upserts on the timestamp.

## Tests

```bash
pytest -q
ruff check .
```

Both run on every pull request, along with a Docker build and a container
smoke test.

## Docker

```bash
docker build -t weatherpage .
docker run -p 8080:8080 -v weather_data:/data weatherpage
```

Pushes to `main` build and publish `ghcr.io/dwzg/weatherpage`, tagged both
`latest` and with the commit SHA, then trigger a Portainer webhook to redeploy.

## Development notes

See [CLAUDE.md](CLAUDE.md) for the conventions that are easy to break —
especially the timestamp format, the cache invalidation rule and the
frontend's single-source-of-truth requirements.
