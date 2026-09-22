/* Live updates: refresh the current-conditions cards once a minute. */

import {
    METRICS, fetchJSON, signed, formatNumber, formatDateTime, formatRelative, t,
} from './format.js';
import { cachedSeries, refresh24h, renderSparklines } from './charts.js';
import { tempToColor } from './heatmap.js';

const POLL_INTERVAL_MS = 60_000;

/* The sparklines show the most recent slice of the 24h series rather than
   asking the server for a separate 3h window. */
const SPARK_WINDOW_MS = 3 * 60 * 60 * 1000;

/* Where the series currently on screen ends. Readings arrive every five
   minutes and this polls every sixty seconds, so four polls in five have
   nothing new to draw — and the 24h series is 25 KB, by an order of magnitude
   the largest thing this page fetches. Derived from the series itself rather
   than from the status that prompted the fetch, so a failed or short response
   leaves it where it was and the next poll tries again. */
let seriesTimestamp = null;

/* How many polls in a row have to fail before the page says so. One failure
   is a blip — a dropped wifi frame, a container restarting mid-deploy — and
   announcing it would be noisier than useful. Two is a little over a minute
   of silence, by which point the numbers on screen really are unverified. */
const OFFLINE_AFTER_FAILURES = 2;

let failedPolls = 0;

const endsAt = (series) =>
    series && series.readings.length
        ? series.readings[series.readings.length - 1].timestamp
        : null;

/** Draw a freshly fetched 24h series into the charts and the sparklines. */
function drawSeries(series) {
    if (!series || !series.readings.length) return;
    refresh24h(series);
    const cutoff = Date.now() - SPARK_WINDOW_MS;
    const recent = series.readings.filter(
        (r) => new Date(r.timestamp.replace(' ', 'T')).getTime() >= cutoff,
    );
    renderSparklines(recent.length ? recent : series.readings.slice(-36));
    seriesTimestamp = endsAt(series);
}

const setText = (id, text) => {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
};

function setValue(id, value, digits, unit) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = formatNumber(value, digits);
    const unitEl = document.createElement('span');
    unitEl.className = 'unit';
    unitEl.textContent = unit;
    el.appendChild(unitEl);
}

/** Show a "vs 24h ago: +1.4°C" line, coloured by direction. */
function setComparison(id, delta, digits, unit, hours, inverted) {
    const el = document.getElementById(id);
    if (!el) return;

    if (delta === null) {
        el.textContent = '';
        el.className = 'detail detail-compare';
        return;
    }

    el.textContent = t(`vs {hours}h ago: {delta}${unit}`,
        { hours, delta: signed(delta, digits) });
    const suffix = inverted ? '-hum' : '';
    const direction = delta > 0 ? `delta-up${suffix}` : delta < 0 ? `delta-down${suffix}` : '';
    el.className = `detail detail-compare ${direction}`.trim();
}

function setBanner(id, html, className) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = '';
    if (!html) return;
    const span = document.createElement('span');
    span.className = className;
    span.textContent = html;
    el.appendChild(span);
}

/* The arrow on a current-conditions card. The template's trend_arrow macro
   builds the same markup from the same fields — including the window, which
   comes off the trend rather than from either side's own constant, so the
   three cards do not start disagreeing about how far back they looked. */
function setTrend(id, trend, digits, unit) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = '';
    if (!trend) return;

    const arrow = { rising: '↑', falling: '↓' }[trend.direction] || '→';
    const span = document.createElement('span');
    span.className = `trend-${trend.direction}`;
    span.textContent = `${arrow} ${signed(trend.delta, digits)}${unit} `;
    const over = document.createElement('span');
    over.className = 'trend-window';
    over.textContent = t('/{hours}h', { hours: trend.hours });
    span.appendChild(over);
    el.appendChild(span);
}

/* Everything on this page is a measurement, and a page that cannot reach the
   server has stopped knowing whether its measurements are current. Left
   unsaid, the values simply freeze and go on looking live — which is the one
   failure this dashboard should not have. */
function setOffline(failing) {
    setBanner(
        'label-offline',
        failing ? t('⚠️ Not reachable — these readings may be out of date') : '',
        'banner banner-warn',
    );
}

async function pollStatus() {
    const status = await fetchJSON('/api/weather/status');

    if (status) {
        failedPolls = 0;
        setOffline(false);
    } else {
        failedPolls += 1;
        if (failedPolls >= OFFLINE_AFTER_FAILURES) setOffline(true);
        return;
    }

    applyStatus(status);

    /* The series is worth refetching only once a reading has actually landed.
       Its own newest point is the one in /status, so that timestamp says
       whether there is anything new to draw. */
    const latest = status && status.current ? status.current.timestamp : null;
    if (latest !== null && latest === seriesTimestamp) return;

    drawSeries(await fetchJSON('/api/weather/history?period=24h'));
}

function applyStatus(status) {
    const { current } = status;
    const byKey = Object.fromEntries(METRICS.map((m) => [m.key, m]));

    setValue('val-temp', current.temperature, byKey.temperature.digits, '°C');
    setValue('val-hum', current.humidity, byKey.humidity.digits, '%');
    setValue('val-pres', current.pressure, byKey.pressure.digits, 'hPa');

    setText('detail-heat', status.heat_index != null
        ? t('Feels like {value}°C', { value: formatNumber(status.heat_index, 1) }) : '');
    setText('detail-dew', status.dew_point != null
        ? t('Dew point {value}°C', { value: formatNumber(status.dew_point, 1) }) : '');

    const hours = status.comparison_hours;
    const past = status.yesterday;
    setComparison('detail-temp-24h',
        past ? current.temperature - past.temperature : null, 1, '°C', hours, false);
    setComparison('detail-hum-24h',
        past ? current.humidity - past.humidity : null, 0, '%', hours, true);

    setTrend('detail-temp-trend', status.temperature_trend, 1, '°C');
    setTrend('detail-hum-trend', status.humidity_trend, 0, '%');
    setTrend('detail-pres-trend', status.pressure_trend, 1, ' hPa');

    /* The API stays in English — the forecast phrase is the same identifier
       app.weather.forecast_emoji() matches on — so the translation happens
       here, against the catalogue the server rendered the page from. */
    const forecast = status.forecast && status.forecast !== 'Not enough data'
        ? `${status.forecast_emoji} ${t(status.forecast)}` : '';
    setBanner('label-forecast', forecast, 'banner');
    updateNowcast(status.nowcast);
    updateDeepDive(status);
    setBanner('label-frost',
        status.frost_warning ? t('❄️ Frost warning — protect your plants!') : '',
        'banner banner-alert');
    setBanner('label-stale',
        status.stale ? t('⚠️ No new readings — the sensor feed may be down') : '',
        'banner banner-warn');

    updateTimestamp(current.timestamp, status.age_seconds);
    updateTab(current.temperature);
}

/* The current temperature in the tab, so a pinned or backgrounded tab
   answers the question without being opened — which is most of what anyone
   wants from this page.

   The favicon is drawn rather than fetched: sixty-odd degrees would be sixty
   files, and the page would then be asking the network for something it
   already knows. Drawn at 64px because a tab icon is 16 or 32 CSS pixels and
   every phone and most laptops render it at 2x. */
const FAVICON_SIZE = 64;
let faviconCanvas = null;

function paintFavicon(temperature) {
    const link = document.querySelector('link[rel="icon"]');
    if (!link || typeof document.createElement !== 'function') return;

    if (!faviconCanvas) {
        faviconCanvas = document.createElement('canvas');
        faviconCanvas.width = faviconCanvas.height = FAVICON_SIZE;
    }
    const ctx = faviconCanvas.getContext('2d');
    if (!ctx) return;

    // Same scale the calendar colours days by, so a glance at the tab and a
    // glance at the calendar mean the same thing.
    ctx.clearRect(0, 0, FAVICON_SIZE, FAVICON_SIZE);
    ctx.fillStyle = tempToColor(temperature);
    ctx.beginPath();
    if (typeof ctx.roundRect === 'function') {
        ctx.roundRect(0, 0, FAVICON_SIZE, FAVICON_SIZE, 12);
    } else {
        ctx.rect(0, 0, FAVICON_SIZE, FAVICON_SIZE);  // older Safari
    }
    ctx.fill();

    const text = `${Math.round(temperature)}°`;
    ctx.fillStyle = '#ffffff';
    ctx.font = `bold ${text.length > 3 ? 30 : 38}px system-ui, sans-serif`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(text, FAVICON_SIZE / 2, FAVICON_SIZE / 2 + 2);

    try {
        link.href = faviconCanvas.toDataURL('image/png');
    } catch {
        /* A tainted or unavailable canvas is not worth a broken page. */
    }
}

function updateTab(temperature) {
    const title = document.title.replace(/^-?\d+°\s·\s/, '');
    document.title = `${Math.round(temperature)}° · ${title}`;
    paintFavicon(temperature);
}

/* "Last updated 4 minutes ago", with the exact timestamp kept in the title
   and in datetime= for anyone who wants it. Same shape the render produced,
   from the same formatters — see i18n.js. */
function updateTimestamp(timestamp, ageSeconds) {
    const el = document.getElementById('label-updated');
    if (!el) return;
    const relative = formatRelative(ageSeconds);
    const exact = formatDateTime(timestamp);
    el.textContent = t('Last updated {ago}', { ago: relative ?? exact });
    el.setAttribute('datetime', timestamp.replace(' ', 'T'));
    el.title = exact;
}

/* The learned rain chance, beside the rule-based phrase. The element is only
   present when the image shipped with a model and the station has enough
   history to feed it, so this adds and removes it rather than assuming it. */
function updateNowcast(nowcast) {
    const row = document.getElementById('label-forecast');
    if (!row) return;
    let el = document.getElementById('label-nowcast');

    if (!nowcast) {
        if (el) el.remove();
        return;
    }
    if (!el) {
        el = document.createElement('span');
        el.id = 'label-nowcast';
        el.className = 'banner banner-nowcast';
        el.title = t("Learned from this station's own history");
        row.appendChild(el);
    }
    el.textContent = '';
    el.append(`🤖 ${t('Rain nearby')} ${t(nowcast.label)} `);
    // Same shape as the server render: the chance is one unwrappable unit.
    const chance = document.createElement('span');
    chance.className = 'nowcast-chance';
    const value = document.createElement('strong');
    value.textContent = `${Math.round(nowcast.probability * 100)}%`;
    chance.append('· ', value, ` ${t('in {hours} h', { hours: nowcast.horizon_hours })}`);
    el.append(chance);
}

/* The deep dive repeats live numbers the banners already show, so it has to
   be maintained too — a coefficient table that quietly describes the weather
   from an hour ago is worse than no table. The section is server-rendered and
   may be absent (no model, or not enough history yet), so every step here
   checks before it writes. */
function updateDeepDive(status) {
    const live = document.getElementById('deep-outlook-live');
    if (live) {
        live.textContent = status.pressure_percentile == null ? '' : t(
            'Right now the pressure ranks at {pct}% of its last {days} days.',
            {
                pct: formatNumber(status.pressure_percentile * 100, 0),
                days: live.dataset.days,
            },
        );
    }

    /* The cloud model's live number, when the composed ladder is the one on
       the page. Its fitted scores sit in the paragraph above and do not move
       between polls, which is why only this sentence is rewritten. */
    const skyLive = document.getElementById('deep-sky-live');
    if (skyLive) {
        skyLive.textContent = !status.sky ? '' : t(
            'Right now it puts the chance of a mostly overcast next {hours} hours at {pct}%.',
            {
                hours: status.sky.horizon_hours,
                pct: formatNumber(status.sky.probability * 100, 0),
            },
        );
    }

    /* Which rung the ladder is standing on. The phrase is the identifier the
       API sends, so it matches on data-phrase and translates for display.
       Both ladders render into the same table, so this needs no branch. */
    document.querySelectorAll('.rule-ladder tbody tr').forEach((row) => {
        row.classList.toggle('is-active', row.dataset.phrase === status.forecast);
    });

    updateFeatureTable(status.nowcast);
}

function updateFeatureTable(nowcast) {
    const table = document.getElementById('deep-features');
    if (!table) return;

    const body = table.tBodies[0];
    if (!nowcast || !nowcast.contributions) {
        body.textContent = '';
        return;
    }

    body.textContent = '';
    for (const row of nowcast.contributions) {
        const tr = document.createElement('tr');
        /* The server picked the scale and the decimals; formatting them here
           rather than re-deriving them keeps the poller's numbers the same
           shape as the render's. */
        const value = `${formatNumber(row.value, row.digits, { sign: row.sign })}${row.unit ? ` ${row.unit}` : ''}`;
        appendCell(tr, t(row.label), '');
        appendCell(tr, value, 'numeric');
        appendCell(tr, signed(row.standardised, 1), 'numeric');
        const direction = row.weight > 0 ? 'weight-up' : row.weight < 0 ? 'weight-down' : '';
        appendCell(tr, signed(row.weight, 2), `numeric ${direction}`.trim());
        body.appendChild(tr);
    }

    /* The footer's two numbers are the intercept and the squashed total. The
       total keeps its <strong>, which textContent on the cell would drop —
       the render's markup is the one this has to reproduce. */
    const foot = table.tFoot;
    if (!foot) return;
    const start = foot.querySelector('[data-cell="intercept"]');
    const total = foot.querySelector('[data-cell="total"] strong');
    if (start) start.textContent = signed(nowcast.intercept, 2);
    if (total) total.textContent = `${formatNumber(nowcast.probability * 100, 0)} %`;
}

function appendCell(tr, text, className) {
    const td = document.createElement('td');
    if (className) td.className = className;
    td.textContent = text;
    tr.appendChild(td);
}

/* A hidden tab is not being read, so it is not worth a request a minute —
   a page left open on a phone or a second monitor otherwise polls all day to
   redraw something nobody is looking at. Coming back polls immediately, so
   the first thing a returning reader sees is current rather than however old
   the tab was when they left it. */
let timer = null;

function stopTimer() {
    if (timer !== null) {
        clearInterval(timer);
        timer = null;
    }
}

function startTimer() {
    stopTimer();
    timer = setInterval(pollStatus, POLL_INTERVAL_MS);
}

export function startPolling() {
    /* initCharts() has already fetched the 24h series by the time this runs,
       so adopt it instead of asking for the same 25 KB again. */
    drawSeries(cachedSeries('24h'));
    pollStatus();
    startTimer();

    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            stopTimer();
        } else {
            pollStatus();
            startTimer();
        }
    });
}
