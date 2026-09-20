/* Live updates: refresh the current-conditions cards once a minute. */

import { METRICS, fetchJSON, signed } from './format.js';
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
    el.textContent = value.toFixed(digits);
    const unitEl = document.createElement('span');
    unitEl.className = 'unit';
    unitEl.textContent = unit;
    el.appendChild(unitEl);
}

/** Show a "+1.4°C vs 24h ago" line, coloured by direction. */
function setComparison(id, delta, digits, unit, hours, inverted) {
    const el = document.getElementById(id);
    if (!el) return;

    if (delta === null) {
        el.textContent = '';
        el.className = 'detail detail-compare';
        return;
    }

    el.textContent = `vs ${hours}h ago: ${signed(delta, digits)}${unit}`;
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
        ? `Feels like ${status.heat_index.toFixed(1)}°C` : '');
    setText('detail-dew', status.dew_point != null
        ? `Dew point ${status.dew_point.toFixed(1)}°C` : '');

    const hours = status.comparison_hours;
    const past = status.yesterday;
    setComparison('detail-temp-24h',
        past ? current.temperature - past.temperature : null, 1, '°C', hours, false);
    setComparison('detail-hum-24h',
        past ? current.humidity - past.humidity : null, 0, '%', hours, true);

    updatePressureTrend(status.pressure_trend);

    const forecast = status.forecast && status.forecast !== 'Not enough data'
        ? `${status.forecast_emoji} ${status.forecast}` : '';
    setBanner('label-forecast', forecast, 'banner');
    updateNowcast(status.nowcast);
    setBanner('label-frost',
        status.frost_warning ? '❄️ Frost warning — protect your plants!' : '',
        'banner banner-alert');
    setBanner('label-stale',
        status.stale ? '⚠️ No new readings — the sensor feed may be down' : '',
        'banner banner-warn');

    setText('label-updated', `Last updated: ${current.timestamp}`);
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
        el.title = "Learned from this station's own history";
        row.appendChild(el);
    }
    el.textContent = '';
    el.append(`🤖 Rain ${nowcast.label} · `);
    const value = document.createElement('strong');
    value.textContent = `${Math.round(nowcast.probability * 100)}%`;
    el.append(value, ` in ${nowcast.horizon_hours} h`);
}

export function startPolling() {
    pollStatus();
    setInterval(pollStatus, POLL_INTERVAL_MS);
}
