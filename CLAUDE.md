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
                                            ├── GET    /api/weather/export     ← raw archive, paged (API key)
                                            ├── GET    /api/weather/predictions ← the prediction log, paged (API key)
                                            ├── GET    /healthz                ← container healthcheck
                                            └── GET    /                       ← serves UI
```

### Modules

| File | Responsibility |
| --- | --- |
| `app/config.py` | Every environment variable, read once into a frozen `Settings`. Nothing else touches `os.environ`. |
| `app/clock.py` | The timestamp convention: formatting, parsing, UTC-offset resolution, period cutoffs, month arithmetic. |
| `app/weather.py` | Pure derived values — dew point, heat index, the forecast rules engine, forecast emoji. No I/O. |
| `app/features.py` | The nowcast's feature vector as a pure function of a run of readings and the pressure cycle. The one implementation the app and the trainer both call. |
| `app/nowcast.py` | Loads `app/model.json` (gradient-boosted trees as plain arrays) and evaluates it, with an exact per-feature breakdown. Pure arithmetic — no ML dependency in the image. |
| `app/model.json` | The fitted nowcast, written by CI. Data, not code: treat it as something that might be wrong. |
| `app/verification.json` | How the *deployed* model has actually done, scored weekly from the prediction log. Absent until there is a record to make a claim about. |
| `ml/train.py` | The retraining job. Runs in CI only; with `ml/dwd.py`, the one thing in this project that fetches anything. |
| `ml/dwd.py` | The German weather service's open ten-minute station archive: station lists, the nearest stations, downloads, parsing. Training only. |
| `app/database.py` | All SQLite access: connection pool, migrations, queries, the daily rollup. |
| `app/cache.py` | Memoisation for the aggregates, dropped on every write. |
| `app/backup.py` | Daily `VACUUM INTO` snapshots of the archive, and their retention. |
| `app/models.py` | Pydantic request/response schemas, including the sensor plausibility ranges. |
| `app/services.py` | Assembles the dashboard payload shared by the API and the page render. |
| `app/i18n.py` | English and German: `Accept-Language` negotiation, the whole string catalogue, locale-aware number formatting. |
| `app/api.py` | The `/api/weather` router and the API-key dependency. |
| `app/main.py` | App factory, lifespan, security headers, compression, the `/` and `/healthz` routes, static mounts. |
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
- **The rule ladder reads pressure as a level, not a tendency.** Scored against observed hourly rainfall for the station's own location over 90 days, the barometric tendency *as a threshold* has negative skill at this station (a 6h fall of >1 hPa scored KSS −0.09; inside the wettest conditions rising pressure was followed by rain more often than falling). `get_pressure_percentile()` ranks the current reading against the station's own last 30 days, because a fixed hPa threshold does not transfer between months (CSI 0.05 in one, 0.44 in another). That rank plus humidity is the whole ladder. Don't reintroduce "falling barometer means rain" as a rule — it was measured and it is wrong here.
- **The learned nowcast does read the tendency, and that is not a contradiction.** As one input among the others in a tree model, over 14 years at eight weather-service stations, the pressure changes were the most valuable *group* of signals (dropping all four cost 0.027 Brier skill, more than any other group). What fails is the lone rule; the trees learn which way a change points in which situation — a sharp rise behind a cold front reads very differently from a slow one under a high.
- **The frost banner has two stages, and they are different claims.** Below `FROST_WARNING_C` (2 °C) it reads the current reading, because that is a statement about the number on the card. Below `FROST_WATCH_C` (4 °C) *and* falling it reads the smoothed 3-hour trend instead, because that is a claim about the next few hours and one cold sample is not a night getting colder. `weather.frost_alert()` returns `"Frost"`, `"Frost likely"` or `None` — English identifiers like the forecast phrases, translated by the page. A single threshold at 2 °C only ever announced a frost that had already arrived.
- **Nothing reads pressure as an absolute, because nobody here knows what the absolute is.** A balcony sensor reports either station pressure or a sea-level-corrected value depending on how it was set up, and the archive does not say which; this app never corrects it either way. So the card prints the reading's rank in the station's own last 30 days beside the number, and the explainer prints the archive's own mean against the ~1013 hPa sea-level norm and lets the reader conclude. Both the rule ladder and the nowcast take the percentile, which is why an unknown offset cannot move a prediction. Don't add a sea-level reduction: it would need an altitude this repository deliberately does not hold (see **GitHub Secrets**).
- **The pressure trend is de-tided**, and the percentile depends on that too.
- **Both ends of every trend are medians** over `SMOOTHING_WINDOW_MINUTES`, not single readings, so one noisy sample cannot push a delta across a threshold. Which way a trend *points* has a per-metric deadband (`database.TREND_DEADBAND`): 0.5 suits hPa and °C, 0.5 % of humidity is noise. The arrows are display only; the nowcast takes its changes from `app/features.py`, which uses the same half-hour medians.
- **Ordering is by `database.ORDER_OLDEST_FIRST` / `ORDER_NEWEST_FIRST`, never by `id`.** A backfill inserts old readings with fresh ids, so `ORDER BY id DESC` would make a backfilled row "current". `timestamp` alone is not a total order either: within one repeated local hour the larger UTC offset is the earlier instant, which is the tie-break those constants carry.

## Database access

- A small connection pool (`database.POOL_SIZE`) is opened in the lifespan via `connect()` and closed by `disconnect()`. Use `async with database.acquire() as db:` — don't open your own connection.
- **Migrations run before the pool is opened.** A connection caches the schema it saw at open time, and `ON CONFLICT(timestamp, utc_offset)` is resolved when a statement is prepared, so a connection opened before the unique index existed would reject every upsert for the life of the process.
- The aggregates (`get_stats`, `get_extremes_with_times`, `get_climate_stats`, `get_daily_extremes`, `get_daily_summaries`, `get_history_series`) are `@cached`. Any new write path must call `invalidate_cache()`, or the dashboard will serve stale numbers until the next reading arrives.
- **Whole-day aggregates read `daily_rollup`, not the readings.** Each day is summarised once — count, per-metric sum, min and max, and when each extreme was reached — and the archive-wide questions roll those few hundred rows up instead of scanning a few hundred thousand. Over two years the four aggregates a page render waits on cost 5 ms rather than 315 ms.
  - **Sums and counts, never means.** A month is `SUM(sum) / SUM(readings)`; an average of daily averages would weight a day with an outage the same as a complete one.
  - **Any new write path must refresh it** inside the write lock, before the commit, via `_refresh_rollup()`. A day is recomputed from its readings rather than adjusted in place: an incremental update that drifts from the table it summarises surfaces months later as a wrong all-time record.
  - `_rollup_scope()` decides what can be answered this way — only `all` and `today`, whose bounds fall on day boundaries. `24h` and the other chart periods cut a day in half and read the readings, where the timestamp index makes the scan cheap.
- **`database.py` is one file on purpose**, and stays that way. It is ~1,260 lines, which looks like a module that wants splitting, so here is the measurement rather than the impression. Its five sections — connection handling, schema, the daily rollup, writes, reads — hold 56 functions, and the calls that cross a section boundary are almost entirely reads reaching for the three pool primitives (`acquire`, `_fetch_all`, `_fetch_one`): 22 of them. The rest is writes calling `_refresh_rollup` (7), which is the invariant above, plus one genuine cycle between the migration and the rollup, since the migration builds it.

  Splitting on those seams would move the two rules most easily broken — *migrations run before the pool opens* and *every write path refreshes the rollup inside the lock* — into separate files, where nothing but a comment connects them. Right now they are enforced by proximity: you cannot add a write without `_refresh_rollup` being on the same screen. That is worth more than a smaller file. Revisit if the reads section grows its own subsystem; until then the section banners are the navigation.

- **The pressure cycle is learned in `app/features.py`.** `get_pressure_cycle()` fetches the 90 *whole* days before today and hands them to `features.pressure_cycle()`, the function the trainer runs over the weather service's stations, so the model's pressure features are de-tided identically on both sides. A day counts when 40 of its 48 half-hour slots hold a reading — slots rather than a reading count, so a ten-minute station and the five-minute balcony judge completeness by one rule. Whole days mean the cycle moves at midnight, and today's half-finished day cannot skew its own correction.
- `get_recent_series()` hands the feature code the last 30 days as a `features.Series`, `@cached` like the aggregates so the poller costs one query per new reading rather than one per request.
- `get_history_series()` downsamples: a period whose raw series would exceed `TARGET_CHART_POINTS` is averaged into buckets, and each point then carries `*_min`/`*_max` for the range band. `/api/weather/history` returns `{readings, interval_seconds, bucketed, expected_samples}` — not a bare array. The route goes through `services.history_payload()`, which adds each point's `dew_point`: the temperature chart plots it as a second line, and deriving it server-side keeps the Magnus formula in `weather.py` alone rather than growing a JavaScript twin that can drift from the cards. On a bucketed point it is the dew point of the bucket's mean temperature and mean humidity — the raw rows a mean-of-dew-points would need are exactly what bucketing discarded, and the difference is hundredths of a degree.

## The two predictions

The dashboard shows two, deliberately different in kind, and the page itself
explains the difference to the reader under "How these two predictions work".

| | rule-based outlook | learned nowcast |
| --- | --- | --- |
| lives in | `app/weather.py` | `app/nowcast.py` + `app/model.json` |
| output | a phrase (`Rain likely`) | a probability (`38%`) |
| fitted by | hand, from measured tiers | `ml/train.py`, weekly in CI, on weather-service stations |
| answers | is it settling or deteriorating | chance of ≥0.2 mm here within 6 h |

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
  starting point plus the printed effects is exactly the logit being
  squashed. Each tree walks one path; every step down it moves the tree's
  expected output from a node's value to its child's, and that move is
  credited to the feature the node split on. Summed over all steps of all
  trees the moves telescope to `leaf - root`, so starting point
  (`Model.expected`: the base score plus every root's value) plus effects is
  the logit, to rounding. Internal nodes carry the expected output of the
  leaves beneath them, weighted by the training hours that reached each —
  `ml/train.py::export()` computes it, since scikit-learn does not store it
  in that form. The balcony calibration (below) multiplies the whole sum by
  its slope and adds its intercept, so the starting point and every effect
  are scaled with it and the table still adds up exactly. Everything in it is
  in log-odds for that reason; don't "normalise" it to percentages, which
  would stop it adding up.
- **There is no coefficient column any more, on purpose.** The logistic
  regression had one per feature, and the page printed it beside the effect.
  A tree's response to a signal depends on the others — that is why it is
  trees — so there is no single number that is "what this signal is worth"
  at every hour. The table is signal, value now, effect now; rows are sorted
  by `|effect|` and only that column is coloured. A value that could not be
  measured (its lag fell in an outage) is sent as `None` and printed as a
  dash; the trees still routed it, so its effect is real and in the sum.
- **The server picks the scale and the decimals.** `services.nowcast_breakdown()`
  sends each row's `value`, `digits`, `sign` and `unit` so the render and the
  poller print the same shape — the same rule the rest of the numbers follow.
- **Every retrain measures what each feature is worth.** `ml/train.py::ablations()` refits on the same training years without each feature in turn and scores the same held-out year, and the model card carries the result; the explainer prints it under the same labels as the contribution table. Single-feature costs are small — the signals overlap, so another one usually carries what a dropped one knew — and a row near zero is not by itself a reason to drop a feature. If one sits at zero across many weeks, that is the evidence for removing it from `FEATURES`; the trainer measures, it never drops anything by itself. Sixteen extra fits are most of the run's time. `tests/test_train.py` checks the measurement against synthetic hours where one feature carries the label and another is noise; it needs numpy and scikit-learn, so it skips in CI and runs for whoever is working on the model.
- **The model card is metadata passed through**, not restated in the template,
  so a retrain updates the page without a code change. Anything `ml/train.py`
  did not write comes back `None` and the section is skipped.
- `poll.js` maintains all of it: the live pressure rank, which rung is
  highlighted, and the whole contribution table. Hooks are the `data-cell`,
  `data-phrase` and `data-days` attributes;
  `tests/test_i18n.py::TestDeepDiveStructure` fails if one side renames one.

### How the nowcast is trained and shipped

**It is not fitted to the balcony's archive, and that is the whole redesign.**
Until October 2026 it was a logistic regression fitted to the balcony's own
readings — ~1,600 labelled hours, all of them one summer. It learned that
cool air means dry air (`temp` carried +1.22 per standard deviation against a
22.7 °C training mean) and that a high barometer means no rain, and on the
first wet October evening — sensor at 100 %, temperature down 4.4 °C in 3 h —
it said 11%. Measured on DWD data, a model of that kind fitted to one summer
scores Brier skill 0.08–0.11 over a following year, and *below zero* in winter
against the reanalysis labels it used. Its own 0.296 was a summer-only number.

`ml/train.py` (run by `.github/workflows/retrain.yml`, Mondays) now:

1. picks the `TRAINING_STATIONS` (5) weather-service stations nearest the
   balcony that measure temperature, humidity *and* pressure every ten
   minutes since `TRAINING_SINCE` (2014) — many have no barometer, and are
   passed over — and downloads their files and their co-located rain gauges
   from opendata.dwd.de (`ml/dwd.py`);
2. converts them to the app's conventions (local wall clock, the database's
   ordering, a per-day pressure cycle learned from the 90 whole days before)
   and calls `app.features.compute()` at every hour — the same function the
   app calls — labelling each hour with ≥0.2 mm at that station's gauge in
   the next six hours;
3. fits `HistGradientBoostingClassifier` (`GBM`: 300 trees of 15 leaves)
   on everything but the last `HOLDOUT_DAYS` and scores that held-out year,
   season by season, against the base rate and the rule ladder;
4. scores the same candidate on **the balcony's own archive** from
   `/api/weather/export`, labelled by the nearest gauge — the evaluation fit
   stops before the archive starts, so neighbouring stations cannot leak the
   same storms into it;
5. **calibrates it to the balcony**: a slope and an intercept on the
   log-odds, fitted (Platt) against the gauge on the most recent
   `CALIBRATION_DAYS` (120) of the archive — see below;
6. refits on everything, exports the trees with the calibration, and writes
   `app/model.json` **only if** the candidate clears the gates on *both*
   tests: Brier skill over the base rate ≥ `MIN_SKILL`, AUC at least the
   ladder's, and no more than `MAX_REGRESSION` below the skill the shipped
   model *recorded* when it was fitted. Refusing to ship is a normal outcome.

**Why the calibration exists.** The first real run scored the trees on the
balcony's archive (1,697 hours, rain in 8.5 % of six-hour windows at the
nearest gauge) at AUC 0.807 — better than at most training stations, and
well above the ladder's 0.73 — and Brier skill **−0.116**. Ranking right and
scoring below the base rate is miscalibration, not blindness: the signal
transfers to this sensor, the scale does not. A sun-baked BME280 that holds
dew and sits at 100 % when wet is not a ventilated screen. Two numbers fix a
scale without touching the ranking (a positive slope cannot reorder hours,
and `nowcast.load()` rejects any other), so that is all the balcony's record
is asked to fit; it is far too short — one summer, ~150 wet hours — to be
asked for more. Details that matter:

- It is **scored out of fold**: the archive is cut into `CALIBRATION_FOLDS`
  contiguous stretches, each scored by a calibration fitted on the others,
  and that score is what the archive gate reads. Contiguous, because rain
  comes in spells and a fit on the hours either side of a wet one would be
  marking its own homework. The yes/no figures use the uncalibrated calls,
  which are the same hours either way.
- The **shipped** calibration is fitted on the final model's log-odds over
  the most recent `CALIBRATION_DAYS`, so it follows the season as the record
  grows rather than carrying a summer's sensor into the winter — at the cost
  of lagging a season change by a few weeks. It needs `MIN_CALIBRATION_WET`
  wet hours; short of that, nothing is calibrated and the archive is not
  gated on.
- The operating **threshold** is carried through the calibration
  (`sigmoid(a·logit(t) + b)`), so the same hours fire.
- The trainer also scores **the live model** on the same archive hours. A
  candidate short of `MIN_SKILL` there may still replace it if it has
  positive skill and beats what is live: the first tree model was trained
  where no archive could be scored, and without this it could have stayed up
  behind a bar it would not pass either. That re-score is optimistic for the
  live model wherever its training stations saw the archive's weather, which
  only ever makes replacing it harder.

Measured over 1.1 million station-hours at eight stations (2014 to
September 2024, scored on the two years after), in Brier skill:

| | gauge labels | ERA5 labels |
| --- | --- | --- |
| logistic, old features, one summer | 0.08–0.11 | 0.02–0.08 |
| logistic, old features, 14 years | 0.15 | 0.19 |
| logistic, + rain-signature features | 0.20 | 0.24 |
| gradient-boosted trees, same features | **0.25** | **0.29** |

The shipped bootstrap model (trained on those eight stations, since the
coordinates are not available outside CI) scores 0.226 on its held-out year
— winter 0.22, spring 0.26, summer 0.20, autumn 0.22 — against AUC 0.657 for
the ladder. A model trained on seven stations scores the eighth (0.22–0.27)
as well as one trained on that station's own years, which is why training on
neighbours and serving the balcony is sound; recalibrating on 100 days of the
target station did not help.

**The labels are rain gauges, which reverses an earlier decision on
evidence.** The regression used a 25 km reanalysis because point rain had
measured as unpredictable (AUC 0.715 against 0.831). That was one summer,
ten signals and a weighted sum, scored against a 2 km *forecast model*. With
real gauges, all seasons and trees, point rain ranks as well as area rain
(AUC 0.842 against 0.837; `nowcast.LABEL_CHOICE`), so the page now says "rain
here" and means it.

Things that are load-bearing:

- **One feature path.** `app/features.py` is the only implementation. It
  is pure — `now` and the pressure cycle are arguments — and every window is
  defined in time, never as a count of readings, because the balcony reports
  every five minutes and the stations every ten. Add a feature by adding it
  to `features.compute()`, `features.DECIMALS`, `nowcast.FEATURE_FORMATS` and
  `FEATURES` in `ml/train.py`; never by computing it in the trainer.
- **Features are rounded to a fixed grid** (`features.DECIMALS`), and that
  is part of the model's contract. The trees split between grid values, so
  `export()` stores each threshold to one decimal more and the app, rounding
  the same way, lands on the same side of every split.
- **The export is checked on every run.** It reads scikit-learn's private
  tree arrays (`_predictors`, `nodes`), which a release could change.
  `self_check()` compares the exported trees with `decision_function()` on
  every training row, and walks the app's own evaluator and breakdown on a
  sample, and fails the run on any disagreement rather than shipping a model
  that says something else. The training step runs under `shell: bash` so
  that failure is not swallowed by `| tee`.
- **The regression gate compares recorded skill, not a re-score.** Re-scored
  now, the shipped model would be judged on hours it was trained on, and that
  in-sample advantage measured +0.043 Brier skill — nearly the whole
  tolerance, enough to refuse every honest retrain from then on.
- **The stations are never named.** They are the nearest to the balcony, so
  their ids, names and above all distances would put the location the
  coordinate secrets protect into a public Actions log or a committed file.
  The trainer prints counts, `model.json` carries `"stations": <int>`, the
  workflow caches nothing (an Actions cache is readable by more than this
  job), and `tests/test_api.py::TestStationsStayAnonymous` fails if a
  `print()` in `ml/` touches a station's `id`, `latitude`, `longitude` or
  `km_from`.
- **The trainer reads `/export`, never `/history`.** `/history` downsamples
  above `TARGET_CHART_POINTS`, which is right for a chart and ruinous here:
  on a 92-day archive it turned ~26,500 readings into 734 three-hourly
  averages, and none of the features can be completed from those. Every run
  of the old trainer therefore found **zero** labelled hours, printed "too
  few samples", and exited 0 — green and decorative for months. `/export`
  never downsamples; it is behind the API key, and pages with a
  `(timestamp, utc_offset)` cursor because a timestamp alone is not unique
  across the repeated autumn hour. `tests/test_api.py::TestTrainerReadsTheRawArchive`
  fails if a `/history` URL reappears in the trainer.
- **The image carries no ML dependency.** `app/nowcast.py` walks the trees
  in pure Python; numpy and scikit-learn live in `ml/requirements.txt` and
  are installed only by the retraining job. `model.json` is ~210 KB.

The sky model goes through the same pipeline, labelled by ERA5 cloud cover
at each training station (and at the balcony, for its archive test). Over
the held-out year it scores well (Brier skill +0.26 at two stations), but on
a summer-only stand-in archive it failed the archive gate, so expect the
weekly job to keep refusing it until the balcony's record covers more than
one season.

A `GITHUB_TOKEN` push does not start another workflow, so the retraining job
cannot deploy by committing — it calls `deploy.yml` through
`workflow_dispatch` explicitly. Remove that trigger and retrained models will
sit on `main` undeployed.

### Verifying it against what actually happened

Everything above scores a *candidate* against a held-out past. That says the method works. It does not say the model **already deployed** has been right about this station's weather, and until now nothing could answer that.

- **`prediction_log`** records what the page showed, hour by hour, written at the time: the rain probability, the sky probability, the phrase from the ladder, and `model_trained_at`. Keyed on `(timestamp, utc_offset)` like the readings, so the repeated autumn hour holds both of its predictions.
- **It cannot be reconstructed afterwards**, which is the whole reason it is a table rather than a query. The features are a pure function of the readings, so a replay could recompute them — but it would credit every past hour to *today's* model, and the model is refitted weekly; a backfill changes the inputs a replay would see; and the container may lag `main`. The `model_trained_at` column is what a replay could never supply.
- **Written on the ingest path, on the hour only**, because the observations it is scored against are hourly — a row every five minutes is twelve times the rows and not one extra scoreable hour. Skipped for a reading that is not the newest, since `build_status()` describes *now* and a backfill would file today's prediction under last week.
- **What is logged comes back through `build_status()`**, not from recomputing anything, so the row is by construction what `/status` served and the page rendered. A log that can disagree with the page is worse than no log.
- **A failure to log is a warning, never a 500.** The reading is the irreplaceable thing and is committed first; handing the relay an error would make Home Assistant retry a reading that was already stored.
- **`ml/train.py` scores it** against the nearest weather-service rain gauge — the same kind of observation the model is trained on, and the same gauge that labels the balcony's archive — and writes `app/verification.json` **outside the shipping decision**, every run. Each logged hour is put on the UTC clock with its `utc_offset` before it is matched to the gauge. The gauge must be within `MAX_GAUGE_KM`; further than that, "rain here" would be scored against elsewhere and nothing is written. Refusing to ship is normal, and a verification that went stale behind a declined candidate would be most misleading exactly when it mattered.
- **The bins live in `app/nowcast.py`**, not in the trainer that writes them: the page renders them, and as pure arithmetic they are testable in the ordinary suite without numpy. A bin below `RELIABILITY_MIN_BIN` hours is shown, dimmed, with its count — hidden thin bins are how a reliability curve flatters itself.
- The page renders it in the technical details and **the poller never touches it**: it changes weekly, in CI, and a new image is what carries it.

### Calibrating the forecast

The rules in `app/weather.py` were fitted offline against observed hourly precipitation for the station's own location (Open-Meteo, ERA5 archive plus the 2 km ICON series as an independent check), joined to the station's own readings. The coordinates live in the `STATION_LATITUDE` / `STATION_LONGITUDE` secrets, not in this repository — see **GitHub Secrets**. **The app fetches nothing at runtime and must stay that way** — the page is self-contained by design. If you retune, score phrases by their observed rain-within-6h frequency rather than by eye: the current tiers run 69% / 44% / 32% / 28% down to 4% against a 21% base rate, and the binary "says rain" forecast scores CSI 0.27 (KSS +0.27), where the rules it replaced scored KSS −0.074 — worse than saying nothing.

## Frontend conventions

- No build step, no framework, no bundler. The page loads `static/js/main.js` as an ES module; Chart.js 4 and its date-fns adapter are **vendored into `static/vendor/`** and served from the image, so the page loads nothing from anywhere else. `tests/test_api.py::TestThePageIsSelfContained` fails if an `https://` `src` or `href` reappears in the markup. See `app/static/vendor/README.md` for the versions and how to update them. The `.chart-fallback` note is kept for a library that somehow does not load — the page still renders values, records and the calendar without it — but it is no longer the expected outcome of being offline.
- **Asset URLs are root-relative (`/static/...`), deliberately.** `url_for()` builds an absolute URL from the request, which behind the HTTPS reverse proxy comes out as `http://` and is blocked as mixed content, leaving the page with no CSS and no JS.
- The page is server-rendered, then `poll.js` updates the same elements every 60 s. Anything the server renders *and* the poller rewrites must have one source of truth: the forecast emoji is computed server-side and sent in `/status`, and the climate card's year pages are rendered server-side with the pager only scrolling between them.
- Charts use a **time scale**; `toTimeData()` inserts a `NaN` point when consecutive points are more than three intervals apart, so outages show as breaks of proportional width. The threshold comes from the response's `interval_seconds`, so a bucketed series doesn't read as one long outage.
- Axis ticks are labelled by `tickFormatter`, which picks decimals from the tick step — pressure spans ~2 hPa a day and would otherwise repeat the same whole number.
- Chart **dates** are formatted with `Intl.DateTimeFormat` through `i18n.dateFormat()`, not with the date-fns adapter's patterns: the adapter bundle ships English only. The adapter is still loaded — the time scale needs it to generate ticks — but `ticks.callback` and the tooltip `title` callback override every label it would produce. The clock stays 24-hour in both languages.
- Sparklines are `responsive: true` inside a fixed-size `.sparkline-wrap`. Sizing the canvas directly does not work: Chart.js writes an inline width onto it, which overrides the stylesheet and stops the card shrinking.
- **Grid overflow gotcha:** grid items default to `min-width: auto`, so a card whose content has a wide minimum pushes the grid past the viewport. `.card` sets `min-width: 0`. The same failure one level down is why every table in the explainer sits in a `.table-scroll` wrapper: a four-column table narrower than a phone scrolls inside its own box instead of dragging the page sideways.
- Card spacing: `.stats-card` has no bottom margin because inside `.stats-grid` the grid `gap` does the spacing; `.container > .stats-card` adds its own.
- **"Is today unusual?" is answered against the same hours of recent days**, not against this date in other years. `database.get_today_anomaly()` cuts every one of the last `ANOMALY_DAYS` at the current time of day and compares today's mean with their median: at 09:00 a whole-day comparison would be measuring the hour, not the weather, and a one-year archive has no other year to ask. A past day counts only with `ANOMALY_COVERAGE` of today's readings, so an outage morning is not "a cold day". It is rendered into the Today's Records card and never polled — it moves over hours.
- **The climate chart draws the whole archive's month range behind the year on show**, from `monthly_all`, which is the only place that pooled climatology is allowed on the page: it was once the chart's own line, disagreeing with the per-year table below it. It is embedded only when the archive holds more than one year, since with one the band traces the line exactly and reads as a drawing bug. `tests/test_i18n.py::TestClimateFollowsTheYear` fails if `monthly_all` appears anywhere but `#climate-norm`.
- **The calendar and the climate year are one pager, not two.** `static/js/pager.js` owns the scroll-snap paging — arrows, swipe, arrow keys, and the height interpolation that keeps a track from being as tall as its tallest page — and the markup carries `.pager-nav` / `.pager-track` / `.pager-page` beside whatever the section calls itself. The height interpolation in particular is fiddly enough that a second copy would drift.
- **The climate card plots one year, and the pager turns it.** The chart used to show `monthly_all` — the climatology, pooled over every year — with a per-year table underneath, so hovering a point and reading the row below gave two different numbers for the same month. Now the render embeds `monthly_by_year` in `#climate-data` and `climate.js` swaps the datasets in place as the page turns, so the chart and the label always agree. Each page carries only the yearly average and min/max, which is the one thing hovering a month cannot answer. Two consequences worth keeping: the y-axis is pinned across all years from the whole archive (`sharedAxis()`), or Chart.js would refit it per year and turning the page would compare nothing; and the months are now only readable by hover or tap, which is the trade that was made when the table went.
- **The calendar asks for the archive's own span, not a fixed two years.** The render puts it on the track as `data-months` (from `database.get_archive_months()`, capped at `MAX_CALENDAR_MONTHS` = 120) and `heatmap.js` reads it, the way the percentile line reads `data-days`. A literal in the module is what made this a bug in waiting: on the first day of the twenty-fifth month the oldest page would simply have stopped existing. `/api/weather/daily` bounds `months` by the same constant, so the ceiling can never sit below what the page asks for. Past the cap the card says so in a footnote rather than starting late in silence — the records and the climate card still read the whole archive.
- **The column widens on a desktop; the calendar does not.** Above 1240px `.container` goes to 1180px, because width is the axis a time series reads on — the charts were 858px of a 1920px screen, a 4:1 strip. The prose keeps its own `ch` caps and `.heatmap-grid` is capped at 860px inside the wider card, so a day cell stays the ~117px the rule below was measured against. Measured at 1920/1440/1280/1100/900/390: nothing below 1240px moves, and no width overflows.
- **The calendar's hover grow is bounded by the gutter it grows into.** `scale(1.04)` on a cell that is at most ~117px wide is 2.3px a side against a 3px half-gap, and `.heatmap-grid` carries 4px of padding because the track clips on both axes (`overflow-y: hidden`, and `overflow-x` cannot scroll left of zero). It was `scale(1.08)`, which put a hovered day on top of both its neighbours and past the card's edge. If you change either number, measure the result rather than eyeballing it.

## Languages

The page is English and German, and nothing else. The language is decided
once, server-side, in this order: `?lang=` on the URL, then the `lang`
cookie a previous `?lang=` set, then `Accept-Language`, then English.

- **The header is a guess, and it is wrong more often than it looks.**
  `Accept-Language` is not built from the OS *display* language but from the
  browser's preferred-languages list, which the OS **region** seeds: an
  English Windows set to Germany asks for German. That is the case the
  precedence above exists for, and the EN/DE switch in the page header is
  just a link to `?lang=`.
- **Only an explicit choice is remembered.** The visit that carries `?lang=`
  sets the cookie (a year, `Path=/`, `HttpOnly`, `SameSite=Lax`); a
  negotiated language is never written back. So the cookie always records
  something a person did rather than something this code guessed — which is
  also what keeps it a preference rather than something to ask consent for.
  `i18n.chosen_language()` is what tells the two apart, and `?lang=fr` sets
  nothing at all. The page's `Vary` names `Cookie` alongside
  `Accept-Language` because the cookie now helps decide the markup.

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

An empty database renders the "waiting for first reading" placeholder, so seed some rows before working on the UI. Several sections only appear with enough history: the climate card needs complete days, and the calendar needs daily summaries.

## Testing

`pytest` and `ruff` run on every pull request **and on every push to `main`** (`.github/workflows/ci.yml`), alongside a Docker build and a container smoke test.

```bash
pytest -q
ruff check .
```

The suite's size is deliberately not written down here. It said 361 while the real figure was 403, which is what a number in prose does: nothing enforces it, so it silently stops being true. Anywhere a measured figure is load-bearing, assert it instead — `weather.CALIBRATION` beside the thresholds it was measured against, `TestRuleLadder` against `compute_forecast()`, `TestDeepDiveStructure` across the template and the ES modules. If a number here cannot be asserted, it does not belong here.

`tests/conftest.py` gives each test its own `DATA_DIR`, a fixed timezone and a clean settings cache. Use the `db` fixture for the storage layer and `client` for anything that goes through HTTP.

For UI changes, drive the page in a real browser (Playwright is available). Measuring the DOM — element boxes, computed styles, console errors — is more reliable than eyeballing a screenshot for spacing work. Chart.js is served from the app itself, so the charts render in a sandbox with no network at all; nothing needs intercepting.

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

## Backups

The readings are the only thing here that cannot be rebuilt: code is in git, the model can be refitted, the image can be rebuilt from a tag, but a reading that was deleted is gone. There are two copies, on purpose, because they fail differently.

- **Daily, in the container** (`app/backup.py`). A `VACUUM INTO` snapshot at 03:30 local into `<DATA_DIR>/backups/weather-YYYY-MM-DD.db`, keeping the last `KEEP_BACKUPS` (7). `VACUUM INTO` rather than a file copy: it is SQLite's supported way to snapshot a live database, it is safe against a concurrent writer, it resolves the WAL, and the copy is defragmented — 500 days came out at 18.8 MB against 23.7 MB, in 82 ms. It refuses to overwrite, so a restart does not rewrite a snapshot it already has, and every failure is logged and swallowed: a broken backup must never be why the dashboard stops serving. The scheduler is an asyncio task in the lifespan, started after the pool (it borrows a connection) and cancelled before it closes.

  This covers a mistaken `DELETE /api/weather/cleanup`, a corrupted page, a bad migration. It does **not** cover losing the volume, because the snapshots are on it.

- **Nightly, off-host** (`.github/workflows/backup.yml`). Pages the whole archive out of `/api/weather/export` and keeps it as a workflow artifact for 90 days. It reads `/export`, never `/history` — the same trap `ml/train.py` fell into, and a "backup" of bucket averages would look fine until someone needed it. `tests/test_backup.py::TestTheOffHostBackupUsesTheRawArchive` fails if a `/history` URL appears there. It follows the `(timestamp, utc_offset)` cursor to the end, and fails loudly on an empty export rather than storing a zero-byte file and reporting success.

**Nothing else watches the feed.** `/healthz` reports `ok` while the sensor is dead — it only says the container can reach its database — so `.github/workflows/feed-check.yml` asks `/api/weather/status` every half hour and fails when `age_seconds` exceeds 45 minutes. A failed workflow run is the alert: GitHub emails the repository owner, exactly as it does for a failed retrain or deploy. That is the whole mechanism, on purpose — no third-party push service, no webhook, no new secret. It reads `age_seconds` rather than computing the age itself, because the stored timestamp is a naive local wall clock and only the app knows which clock it belongs to.

To restore: stop the container, put the snapshot in place of `/data/weather.db` (removing any `-wal`/`-shm` beside it), start it again. The migration is idempotent and will rebuild anything missing. From the off-host NDJSON instead, replay it into `POST /api/weather` — the ingest upserts on `(timestamp, utc_offset)`, so a replay over a partially recovered database converges rather than duplicating.

## Deployment

Deployed via Docker Compose managed by Portainer. The compose file lives in a separate repo at `~/git/docker-compose/weatherpage/docker-compose.yml`:
- Image: `ghcr.io/dwzg/weatherpage:latest`
- Volume: `/opt/docker/weatherpage:/data` (persists SQLite DB)
- Network: external `nginx-proxy-network`

Every push to `main` builds and pushes a new image, then triggers the Portainer webhook to pull and restart. Images are also tagged with the commit SHA, so a bad deploy can be rolled back by pinning the previous SHA in the compose file.

**Assets are versioned by the path, not by a `?v=` query, and by the commit, not by `__version__`.** Both halves of that were learned the hard way.

`__version__` sat at `2.0.0` across every deploy, so `?v=2.0.0` never changed and browsers kept serving assets from *before* a release. `ASSET_VERSION` still overrides if you need to pin it.

Versioning the query string then fixed only the entry point. The page hands the browser `main.js?v=<sha>`, but the `./heatmap.js` it imports resolves **relative to that URL** and comes out unversioned, so the browser goes on serving whatever it cached the first time it saw the page. The result is a page with new markup, new CSS and a new `main.js` driving months-old modules — which renders *wrong*, not stale: the calendar stopped paging and the climate chart never drew, because the modules were building markup the stylesheet no longer had rules for. Serving from `/static/<version>/…` fixes it at the root, because a relative import inherits the directory, so the whole graph is versioned with no build step. The plain `/static` mount stays for anything holding an old link; the page is `no-store`, so it always hands out the current prefix.

`tests/test_api.py::TestDashboard::test_every_module_the_page_loads_is_versioned` walks that import graph the way the browser does and fails if any module resolves unversioned. Adding a module needs nothing — it is reached through the graph.

Because the path names the build, the versioned mount serves `Cache-Control: public, max-age=31536000, immutable` — the browser then never revalidates, which is the payoff the path versioning was for. With one guard: outside a built image nothing sets `GIT_SHA`, the version falls back to `__version__`, and `/static/2.0.0/…` stands still across releases. Promising a year there would restage the original bug in a development browser, so `Settings.asset_version_names_a_build` records whether the version identifies a build at all and the header is only claimed when it does. The plain `/static` mount never claims it.

**`GET /healthz` reports the commit the running container was built from.** That is how to tell a stalled deploy from a fresh one: the version string moves rarely, so without it the two look identical from outside. The Dockerfile takes it as `GIT_SHA` and both workflows pass `${{ github.sha }}`; it reads `unknown` outside a built image.

**The image must be pushed as a plain manifest, not an index** — `provenance: false` and `sbom: false` on `build-push-action`. The container driver `setup-buildx-action` provides attaches a provenance attestation by default, which makes the tag an OCI index whose children are *untagged* package versions. `delete-untagged-action` then deletes them, leaving a tag that still resolves while every pull fails with `manifest unknown`. That cleanup step also now runs **before** the push rather than after, so it can never race the webhook for the image just built. Since the deploy only runs post-merge, a failure lands on `main` with nothing on the PR to show it: the webhook step is skipped and the old image keeps serving. The GHCR push occasionally fails with `ERROR: unknown blob`, which is a transient registry error — re-run the failed job.

## GitHub Secrets

- `APP_URL` — base URL of the running app. Used by the relay workflow (`main.yml`) to forward HA webhook data.
- `STATION_LATITUDE` / `STATION_LONGITUDE` — where the station stands, read by `ml/train.py` to choose the nearest weather-service stations to train on and the nearest rain gauge to score the balcony against, and to ask Open-Meteo for the cloud cover the sky model's archive test uses. **They are deliberately not in the repository**, and the trainer has no fallback: a default would either be wrong, and quietly train on another place's weather, or be the real location, which is what these keep out of a public file. Nothing derived from them — station ids, names, distances — is printed or committed either. An unset secret fails the retraining run. To run the trainer locally, export them or pass `--latitude` / `--longitude`; `--stations <ids>` trains on named stations instead and, without a location, skips the archive and the verification.
- `API_KEY` — shared secret protecting `POST /api/weather`, `DELETE /api/weather/cleanup` and `GET /api/weather/export`. The retraining workflow passes it too, since the trainer reads the export endpoint. Must match between the app (env var), the relay workflow, and (eventually) Home Assistant. When the env var is unset those endpoints are unauthenticated, which is how local dev works.
- `PORTAINER_WEBHOOK_URL` — Portainer webhook URL triggered by `deploy.yml` after a successful image push.
- `GITHUB_TOKEN` — auto-provided, used for GHCR login and push.
- `DELETE_PACKAGES_TOKEN` — personal access token with `delete:packages` scope for cleaning old untagged images.

Workflow inputs are passed to the shell through `env:`, never interpolated into a `run:` body — `${{ inputs.x }}` is substituted before the shell sees it, so a crafted value would execute as shell on the runner.

## Known limitations

- **The balcony's hygrometer saturates in rain; a weather-service screen's does not.** In rain the screens read a median 90–94 % and reach ≥97 % in only 2–17 % of rain intervals, so in training a saturated sensor mostly means fog. The BME280 on the balcony sits at exactly 100 % when wet. Together with the sun this is why the trees run high here; the calibration corrects the scale, but "Time saturated, 3 h" still carries nothing, which on this sensor it should. Correcting the inputs themselves — learning the sensor's own humidity and temperature against the nearest station — is the next step once the archive spans more than one season. The calibration is fitted on a single season too, and will lag the next one by a few weeks.
- **Stations without a barometer are skipped after one small download.** `dwd.has_barometer()` reads the recent air file first; most of the service's ten-minute stations carry no pressure at all (13 were skipped on the first real run, each of which used to cost its full history and its gauge).

- `get_history_series()` still scans the readings, and is now the most expensive query on a long archive (~120 ms over two years). It is the one aggregate a per-day rollup cannot serve: a chart needs resolution finer than a day until the archive is long enough for the bucket ladder to reach 1440 minutes.
- A backfill cannot restore the *first* pass of a repeated autumn hour after the fact, because walking wall-clock time only ever visits 02:30 once and a replay has no arrival time to resolve it with. Posting the timestamp with an explicit `+02:00` is the way to aim at it.
- The spring-forward gap is the mirror image: 02:30 never happens, and a reading claiming it is read as pre-transition rather than rejected.

## Future: Direct Home Assistant Integration

When ready, change Home Assistant to POST directly to `https://<app-url>/api/weather`. The GitHub Actions relay (`main.yml`) can then be removed.
