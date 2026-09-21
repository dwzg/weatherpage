/* Live updates: refresh the current-conditions cards once a minute. */

import { METRICS, fetchJSON, signed, formatNumber, t } from './format.js';
import { refresh24h, renderSparklines } from './charts.js';

const POLL_INTERVAL_MS = 60_000;

/* The sparklines show the most recent slice of the 24h series rather than
   asking the server for a separate 3h window. */
const SPARK_WINDOW_MS = 3 * 60 * 60 * 1000;

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

function updatePressureTrend(trend) {
    const el = document.getElementById('detail-pres-trend');
    if (!el) return;
    el.textContent = '';
    if (!trend) return;

    const arrow = { rising: '↑', falling: '↓' }[trend.direction] || '→';
    const span = document.createElement('span');
    span.className = `trend-${trend.direction}`;
    span.textContent = `${arrow} ${signed(trend.delta, 1)} hPa`;
    el.appendChild(span);
}

async function pollStatus() {
    const [status, series] = await Promise.all([
        fetchJSON('/api/weather/status'),
        fetchJSON('/api/weather/history?period=24h'),
    ]);

    if (status) applyStatus(status);
    if (series && series.readings.length) {
        refresh24h(series);
        const cutoff = Date.now() - SPARK_WINDOW_MS;
        const recent = series.readings.filter(
            (r) => new Date(r.timestamp.replace(' ', 'T')).getTime() >= cutoff,
        );
        renderSparklines(recent.length ? recent : series.readings.slice(-36));
    }
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

    updatePressureTrend(status.pressure_trend);

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

    setText('label-updated', t('Last updated: {timestamp}', { timestamp: current.timestamp }));
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

export function startPolling() {
    pollStatus();
    setInterval(pollStatus, POLL_INTERVAL_MS);
}
