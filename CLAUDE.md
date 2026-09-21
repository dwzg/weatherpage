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
| `app/clock.py` | The timestamp convention: formatting, parsing, UTC-offset resolution, period cutoffs, month arithmetic. |
| `app/weather.py` | Pure derived values — dew point, heat index, the forecast rules engine, forecast emoji. No I/O. |
| `app/nowcast.py` | Loads `app/model.json` and evaluates it. Pure arithmetic — no ML dependency in the image. |
| `app/model.json` | The fitted nowcast, written by CI. Data, not code: treat it as something that might be wrong. |
| `ml/train.py` | The retraining job. Runs in CI only; the one thing in this project that fetches anything. |
| `app/database.py` | All SQLite access: connection pool, migrations, queries, the daily rollup. |
| `app/cache.py` | Memoisation for the aggregates, dropped on every write. |
| `app/models.py` | Pydantic request/response schemas, including the sensor plausibility ranges. |
| `app/services.py` | Assembles the dashboard payload shared by the API and the page render. |
| `app/i18n.py` | English and German: `Accept-Language` negotiation, the whole string catalogue, locale-aware number formatting. |
| `app/api.py` | The `/api/weather` router and the API-key dependency. |
| `app/main.py` | App factory, lifespan, the `/` and `/healthz` routes, static mount. |
| `app/templates/index.html` | Markup only — no inline CSS or JS. |
| `app/static/css/dashboard.css` | All styles. |
| `app/static/js/*.js` | ES modules: `i18n` (the catalogue the render embedded), `format` (shared helpers), `pager` (the scroll-snap pager both the calendar and the climate year use), `charts`, `heatmap`, `climate`, `poll`, `main` (entry point). |
| `backfill.py` | Standalone script (not in the image) that replays a Home Assistant window to fill an outage. |

## Data conventions

These are implicit across the codebase and easy to break:

- **Timestamps are naive local-time strings**, `YYYY-MM-DD HH:MM:SS`, matching what Home Assistant sends. Nothing is stored in UTC.
- **Every reading also carries `utc_offset`**, minutes east of UTC for the instant it was taken. That is the only thing distinguishing the two passes through the repeated hour of the autumn DST fallback, when 02:00–02:59 names two different hours of weather. It is resolved by `clock.resolve_offset()`: an explicit offset on the incoming timestamp wins, otherwise the arrival time decides (once the second 02:30 has begun, a bare 02:30 is the second one), otherwise the first pass. The stored string keeps its shape — the column exists precisely so the format stays fixed-width and lexicographic.
- **Range queries are lexicographic string comparisons.** Build every bound through `clock.fmt_ts()` / `clock.period_cutoff()` — comparing against an ISO-with-`T` or UTC string silently matches nothing. `clock.normalise_ts()` is the only entry point for incoming timestamps.
- Because the format is fixed-width, queries slice it with `substr()` instead of `strftime()`/`date()`, and a whole day is bounded by the string literals `"<day> 00:00:00"` and `"<day> 23:59:59"` — same results, no date arithmetic, and unlike `substr()` those bounds can use the timestamp index.
- The timezone comes from the `TIMEZONE` env var (default `Europe/Berlin`) via `clock.now()`. Never use a bare `datetime.now()`.
- **`(timestamp, utc_offset)` is UNIQUE.** Ingestion upserts, so re-posting a slot corrects it rather than duplicating it — while the repeated autumn hour still holds both of its readings. On first start against an older database the migration fills in the offsets, de-duplicates (keeping the earliest row per key) and rebuilds the index; a database whose unique index covers `timestamp` alone has it dropped and recreated, since `CREATE INDEX IF NOT EXISTS` would quietly accept the old one.
- **Readings are expected on a 5-minute grid with `:00` seconds.** `remove_off_grid_readings()` deletes anything off-grid; `remove_readings_in_range()` clears a window. Chart gaps are a real signal, so don't "fix" missing slots by interpolating.
- **Incoming readings are range-checked** (`app/models.py`): temperature −90…60 °C, humidity 0…100 %, pressure 800…1100 hPa. A sensor glitch or a Home Assistant `unavailable` is rejected with 422 rather than stored forever.
- **The forecast reads pressure as a level, not a tendency.** Scored against observed hourly rainfall at Rheinfelden over 90 days, the barometric tendency has *negative* skill at this station (a 6h fall of >1 hPa scored KSS −0.09; inside the wettest conditions rising pressure was followed by rain more often than falling). `get_pressure_percentile()` ranks the current reading against the station's own last 30 days, because a fixed hPa threshold does not transfer between months (CSI 0.05 in one, 0.44 in another). That rank plus humidity is the whole forecast; the tendency is still measured and shown on the pressure card, but nothing predictive is built on it. Don't reintroduce "falling barometer means rain" — it was measured and it is wrong here.
- **The pressure trend is de-tided**, and the percentile depends on that too.- **Both ends of every trend are medians** over `SMOOTHING_WINDOW_MINUTES`, not single readings, so one noisy sample cannot push a delta across a threshold.
- **Ordering is by `database.ORDER_OLDEST_FIRST` / `ORDER_NEWEST_FIRST`, never by `id`.** A backfill inserts old readings with fresh ids, so `ORDER BY id DESC` would make a backfilled row "current". `timestamp` alone is not a total order either: within one repeated local hour the larger UTC offset is the earlier instant, which is the tie-break those constants carry.

## Database access

- A small connection pool (`database.POOL_SIZE`) is opened in the lifespan via `connect()` and closed by `disconnect()`. Use `async with database.acquire() as db:` — don't open your own connection.
- **Migrations run before the pool is opened.** A connection caches the schema it saw at open time, and `ON CONFLICT(timestamp, utc_offset)` is resolved when a statement is prepared, so a connection opened before the unique index existed would reject every upsert for the life of the process.
- The aggregates (`get_stats`, `get_extremes_with_times`, `get_climate_stats`, `get_daily_extremes`, `get_daily_summaries`, `get_history_series`) are `@cached`. Any new write path must call `invalidate_cache()`, or the dashboard will serve stale numbers until the next reading arrives.
- **Whole-day aggregates read `daily_rollup`, not the readings.** Each day is summarised once — count, per-metric sum, min and max, and when each extreme was reached — and the archive-wide questions roll those few hundred rows up instead of scanning a few hundred thousand. Over two years the four aggregates a page render waits on cost 5 ms rather than 315 ms.
  - **Sums and counts, never means.** A month is `SUM(sum) / SUM(readings)`; an average of daily averages would weight a day with an outage the same as a complete one.
  - **Any new write path must refresh it** inside the write lock, before the commit, via `_refresh_rollup()`. A day is recomputed from its readings rather than adjusted in place: an incremental update that drifts from the table it summarises surfaces months later as a wrong all-time record.
  - `_rollup_scope()` decides what can be answered this way — only `all` and `today`, whose bounds fall on day boundaries. `24h` and the other chart periods cut a day in half and read the readings, where the timestamp index makes the scan cheap.
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

### The explainer on the page

One card under the banners, opened by a single handle. Inside it the short
answer comes first, and "The technical details" is a `<details>` nested in the
same body — the working: the rule ladder with its measured hit rates, the model
card, the skill table against both baselines, and a live breakdown of what each
feature is contributing to the current probability. It was two sibling
`<details>`, which read as two panels arguing about which one to open. The
summary rules in the stylesheet are child selectors (`.prediction-note >
summary`) precisely so the nested handle does not inherit the card's own.

- **The ladder is generated from the thresholds, not retyped.**
  `weather.RULE_LADDER` describes each rung of `compute_forecast()` and reads
  the same constants, so retuning a threshold moves the page with it.
  `weather.CALIBRATION` carries the measured numbers beside them.
  `tests/test_weather.py::TestRuleLadder` drives `compute_forecast()` with
  inputs for every rung and fails if it answers with a different phrase — the
  ladder is documentation, so nothing else would notice it drifting.
- **The feature breakdown is the prediction, not an illustration of it.**
  `Model.predict()` is routed through `Model.contributions()`, so the
  intercept plus the printed weights is exactly the logit being squashed.
  Weights are in log-odds for that reason; don't "normalise" them to
  percentages, which would stop them adding up.
- **The server picks the scale and the decimals.** `services.nowcast_breakdown()`
  sends each row's `value`, `digits`, `sign` and `unit` so the render and the
  poller print the same shape — the same rule the rest of the numbers follow.
- **The model card is metadata passed through**, not restated in the template,
  so a retrain updates the page without a code change. Anything `ml/train.py`
  did not write comes back `None` and the section is skipped.
- `poll.js` maintains all of it: the live pressure rank, which rung is
  highlighted, and the whole contribution table. Hooks are the `data-cell`,
  `data-phrase` and `data-days` attributes;
  `tests/test_i18n.py::TestDeepDiveStructure` fails if one side renames one.

### How the nowcast is trained and shipped

`ml/train.py` (run by `.github/workflows/retrain.yml`, Mondays) pulls the
station's readings from its own public history endpoint and observed hourly
rainfall from Open-Meteo's ERA5 archive for the labels. It fits a logistic
regression, scores it walk-forward with weekly refits, and writes
`app/model.json` **only if** the candidate clears the gates in that file:
skill over climatology, ranking at least as well as the rules, and no sharp
regression against the shipped model. Refusing to ship is a normal outcome.

**The labels are the 25 km reanalysis on purpose.** Open-Meteo's 2 km series
is also fetched, but only ever scored against. Trained and judged on it the
same features manage AUC 0.715 / CSI 0.220, against 0.831 / 0.473 on the
reanalysis: point rain is 8% of hours and turns on convective detail a
barometer cannot see, while "did it rain around here" is the synoptic
question these sensors answer. So the percentage on the page means rain
**in the area**, and the page says so — judged on point rain the model's
Brier skill is −0.256, because it quotes area odds. Retraining on the
higher-resolution source is the intuitive move and it was measured to be
wrong.

A learned "has it been raining" stage, feeding a persistence-like signal in,
was also tried and dropped: it moved Brier by 0.0007 and AUC by 0.004, which
is noise at this sample size, because the humidity features already carry
that signal.

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
- The page is server-rendered, then `poll.js` updates the same elements every 60 s. Anything the server renders *and* the poller rewrites must have one source of truth: the forecast emoji is computed server-side and sent in `/status`, and every year's monthly table is rendered server-side with the pager only scrolling between them.
- Charts use a **time scale**; `toTimeData()` inserts a `NaN` point when consecutive points are more than three intervals apart, so outages show as breaks of proportional width. The threshold comes from the response's `interval_seconds`, so a bucketed series doesn't read as one long outage.
- Axis ticks are labelled by `tickFormatter`, which picks decimals from the tick step — pressure spans ~2 hPa a day and would otherwise repeat the same whole number.
- Chart **dates** are formatted with `Intl.DateTimeFormat` through `i18n.dateFormat()`, not with the date-fns adapter's patterns: the adapter bundle ships English only. The adapter is still loaded — the time scale needs it to generate ticks — but `ticks.callback` and the tooltip `title` callback override every label it would produce. The clock stays 24-hour in both languages.
- Sparklines are `responsive: true` inside a fixed-size `.sparkline-wrap`. Sizing the canvas directly does not work: Chart.js writes an inline width onto it, which overrides the stylesheet and stops the card shrinking.
- **Grid overflow gotcha:** grid items default to `min-width: auto`, so a card whose content has a wide minimum pushes the grid past the viewport. `.card` sets `min-width: 0`. The same failure one level down is why every table in the explainer sits in a `.table-scroll` wrapper: a four-column table narrower than a phone scrolls inside its own box instead of dragging the page sideways.
- Card spacing: `.stats-card` has no bottom margin because inside `.stats-grid` the grid `gap` does the spacing; `.container > .stats-card` adds its own.
- **The calendar and the climate year are one pager, not two.** `static/js/pager.js` owns the scroll-snap paging — arrows, swipe, arrow keys, and the height interpolation that keeps a track from being as tall as its tallest page — and the markup carries `.pager-nav` / `.pager-track` / `.pager-page` beside whatever the section calls itself. The height interpolation in particular is fiddly enough that a second copy would drift.
- **The calendar's hover grow is bounded by the gutter it grows into.** `scale(1.04)` on a cell that is at most ~117px wide is 2.3px a side against a 3px half-gap, and `.heatmap-grid` carries 4px of padding because the track clips on both axes (`overflow-y: hidden`, and `overflow-x` cannot scroll left of zero). It was `scale(1.08)`, which put a hovered day on top of both its neighbours and past the card's edge. If you change either number, measure the result rather than eyeballing it.

## Languages

The page is English and German, and nothing else. German is served when the
browser asks for it in `Accept-Language`, which is what a German-language OS
sets; `?lang=de` / `?lang=en` overrides it, for testing and for a reader whose
OS disagrees with them.

- **The message id is the English string.** `app/i18n.py` holds one catalogue,
  `GERMAN`, keyed by the English text, so English needs no table and a missing
  translation degrades to English rather than to a bare key.
- **The JSON API stays English.** The forecast phrases are identifiers, not
  prose: `weather.forecast_emoji()` matches on them, `ml/train.py` replays
  against them and the tests compare them. Translating them in `/status` would
  make all three language-dependent, so `poll.js` translates instead.
- **One catalogue, two consumers.** The render embeds the catalogue it used in
  `<script id="i18n-data">` and `static/js/i18n.js` unpacks it. The poller
  rewrites elements the render produced, so a second copy of the strings in
  JavaScript would show up as a page that turns half-English after 60 s.
- **Numbers are formatted from the separators the server sent**, not from
  `Intl.NumberFormat`, for the same reason: German writes 22,5 °C and 208.942
  readings, and a value the server rendered must not change shape when the
  poller rewrites it. `i18n.format_number()` and its JS twin are deliberately
  the same arithmetic.
- `tests/test_i18n.py` scans the template and every ES module for the literals
  they pass to `t()` and fails if one has no German, then renders the whole
  page and fails if a known English string survives. Adding a string without a
  translation is a test failure, not something to notice on the live site.

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
pytest -q          # 340 tests
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

Every push to `main` builds and pushes a new image, then triggers the Portainer webhook to pull and restart. Images are also tagged with the commit SHA, so a bad deploy can be rolled back by pinning the previous SHA in the compose file.

**Asset URLs are versioned by the commit**, not by `__version__`. The app version sat at `2.0.0` across every deploy, so `?v=2.0.0` never changed and browsers kept serving the stylesheet and ES modules from *before* a release — which looks like a broken page rather than a stale one, because the server-rendered markup is new while the JS that maintains it is old. `ASSET_VERSION` still overrides if you need to pin it.

**`GET /healthz` reports the commit the running container was built from.** That is how to tell a stalled deploy from a fresh one: the version string moves rarely, so without it the two look identical from outside. The Dockerfile takes it as `GIT_SHA` and both workflows pass `${{ github.sha }}`; it reads `unknown` outside a built image.

**The image must be pushed as a plain manifest, not an index** — `provenance: false` and `sbom: false` on `build-push-action`. The container driver `setup-buildx-action` provides attaches a provenance attestation by default, which makes the tag an OCI index whose children are *untagged* package versions. `delete-untagged-action` then deletes them, leaving a tag that still resolves while every pull fails with `manifest unknown`. That cleanup step also now runs **before** the push rather than after, so it can never race the webhook for the image just built. Since the deploy only runs post-merge, a failure lands on `main` with nothing on the PR to show it: the webhook step is skipped and the old image keeps serving. The GHCR push occasionally fails with `ERROR: unknown blob`, which is a transient registry error — re-run the failed job.

## GitHub Secrets

- `APP_URL` — base URL of the running app. Used by the relay workflow (`main.yml`) to forward HA webhook data.
- `STATION_LATITUDE` / `STATION_LONGITUDE` — where the station stands, read by `ml/train.py` to ask Open-Meteo for the rainfall that labels the training data. **They are deliberately not in the repository**, and the trainer has no fallback: a default would either be wrong, and quietly label the data with another place's weather, or be the real location, which is what these keep out of a public file. An unset secret fails the retraining run. To run the trainer locally, export them or pass `--latitude` / `--longitude`.
- `API_KEY` — shared secret protecting `POST /api/weather` and `DELETE /api/weather/cleanup`. Must match between the app (env var), the relay workflow, and (eventually) Home Assistant. When the env var is unset those endpoints are unauthenticated, which is how local dev works.
- `PORTAINER_WEBHOOK_URL` — Portainer webhook URL triggered by `deploy.yml` after a successful image push.
- `GITHUB_TOKEN` — auto-provided, used for GHCR login and push.
- `DELETE_PACKAGES_TOKEN` — personal access token with `delete:packages` scope for cleaning old untagged images.

Workflow inputs are passed to the shell through `env:`, never interpolated into a `run:` body — `${{ inputs.x }}` is substituted before the shell sees it, so a crafted value would execute as shell on the runner.

## Known limitations

- `get_history_series()` still scans the readings, and is now the most expensive query on a long archive (~120 ms over two years). It is the one aggregate a per-day rollup cannot serve: a chart needs resolution finer than a day until the archive is long enough for the bucket ladder to reach 1440 minutes.
- A backfill cannot restore the *first* pass of a repeated autumn hour after the fact, because walking wall-clock time only ever visits 02:30 once and a replay has no arrival time to resolve it with. Posting the timestamp with an explicit `+02:00` is the way to aim at it.
- The spring-forward gap is the mirror image: 02:30 never happens, and a reading claiming it is read as pre-transition rather than rejected.

## Future: Direct Home Assistant Integration

When ready, change Home Assistant to POST directly to `https://<app-url>/api/weather`. The GitHub Actions relay (`main.yml`) can then be removed.
