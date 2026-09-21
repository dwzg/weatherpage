/* Main history charts and the per-card sparklines. */

import {
    METRICS, chartStyle, chartsAvailable, parseTimestamp, fetchJSON,
    t, formatNumber, dateFormat, LOCALE,
} from './format.js';

/* Axis formats per period, as Intl.DateTimeFormat options rather than the
   date-fns patterns the adapter would otherwise use: the adapter bundle ships
   English only, while Intl already knows every locale the browser does. The
   clock stays 24-hour in both languages, which is what the page always had. */
const CLOCK = { hour: '2-digit', minute: '2-digit', hour12: false };

const TIME_FORMATS = {
    '24h': { unit: 'hour',  display: CLOCK,                              tooltip: CLOCK },
    '7d':  { unit: 'day',   display: { weekday: 'short' },               tooltip: { weekday: 'short', ...CLOCK } },
    '30d': { unit: 'day',   display: { day: 'numeric', month: 'short' }, tooltip: { day: 'numeric', month: 'short', ...CLOCK } },
    'all': { unit: 'month', display: { month: 'short', year: '2-digit' }, tooltip: { month: 'long', year: 'numeric' } },
};

/* A gap wider than this many point-intervals is an outage rather than
   ordinary spacing, and the line is broken so it shows as a real hole. */
const GAP_INTERVAL_FACTOR = 3;

const charts = {};
const sparkCharts = {};
const historyCache = {};

/**
 * Build chart points, inserting a NaN break wherever consecutive readings sit
 * further apart than the expected interval. Outages then render as gaps of
 * proportional width instead of a straight line across missing days.
 */
function toTimeData(readings, key, gapThresholdMs) {
    const points = [];
    for (let i = 0; i < readings.length; i++) {
        const at = parseTimestamp(readings[i].timestamp);
        points.push({ x: at, y: readings[i][key] });

        if (i < readings.length - 1) {
            const next = parseTimestamp(readings[i + 1].timestamp);
            if (next - at > gapThresholdMs) points.push({ x: at + 1, y: NaN });
        }
    }
    return points;
}

/** Upper and lower bounds of a bucket, drawn as a shaded band. */
function toBandData(readings, key, bound, gapThresholdMs) {
    return toTimeData(
        readings.map((r) => ({ timestamp: r.timestamp, [key]: r[`${key}_${bound}`] })),
        key,
        gapThresholdMs,
    );
}

/**
 * Label a tick with just enough precision to keep neighbouring ticks distinct.
 * Pressure spans a couple of hPa over a day, so whole numbers would repeat the
 * same label several times; humidity spans tens, where decimals are noise.
 */
function tickFormatter(value, index, ticks) {
    const step = ticks.length > 1 ? Math.abs(ticks[1].value - ticks[0].value) : 1;
    const decimals = step >= 1 ? 0 : step >= 0.1 ? 1 : 2;
    return formatNumber(value, decimals);
}

function makeChartConfig(metric, series, period) {
    const style = chartStyle();
    const fmt = TIME_FORMATS[period] || TIME_FORMATS['24h'];
    const gap = series.interval_seconds * 1000 * GAP_INTERVAL_FACTOR;
    const datasets = [];

    // When points are bucket averages, show the spread they hide.
    if (series.bucketed) {
        datasets.push(
            {
                label: t('Min'),
                data: toBandData(series.readings, metric.key, 'min', gap),
                borderColor: 'transparent',
                backgroundColor: metric.color.band,
                pointRadius: 0,
                fill: '+1',
                tension: 0.3,
                spanGaps: false,
            },
            {
                label: t('Max'),
                data: toBandData(series.readings, metric.key, 'max', gap),
                borderColor: 'transparent',
                backgroundColor: metric.color.band,
                pointRadius: 0,
                fill: false,
                tension: 0.3,
                spanGaps: false,
            },
        );
    }

    datasets.push({
        label: metric.label,
        data: toTimeData(series.readings, metric.key, gap),
        borderColor: metric.color.line,
        backgroundColor: series.bucketed ? 'transparent' : metric.color.bg,
        borderWidth: 2,
        fill: !series.bucketed,
        pointRadius: 0,
        pointHoverRadius: 4,
        tension: 0.3,
        spanGaps: false,
    });

    return {
        type: 'line',
        data: { datasets },
        options: {
            locale: LOCALE,
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            plugins: {
                legend: { display: false },
                tooltip: {
                    // The band datasets would just repeat the average's tooltip.
                    filter: (item) => item.datasetIndex === datasets.length - 1,
                    callbacks: {
                        title: (items) => (items.length
                            ? dateFormat(fmt.tooltip).format(items[0].parsed.x) : ''),
                    },
                },
            },
            scales: {
                x: {
                    type: 'time',
                    time: { unit: fmt.unit },
                    ticks: {
                        maxTicksLimit: 8,
                        maxRotation: 0,
                        color: style.tickColor,
                        font: { size: 10 },
                        // The time scale hands its callback the raw timestamp.
                        callback: (value) => dateFormat(fmt.display).format(value),
                    },
                    grid: { display: false },
                },
                y: {
                    ticks: {
                        color: style.tickColor,
                        font: { size: 10 },
                        callback: tickFormatter,
                    },
                    grid: { color: style.gridColor },
                },
            },
            interaction: { intersect: false, mode: 'index' },
        },
    };
}

function destroyCharts() {
    Object.values(charts).forEach((chart) => chart.destroy());
    for (const key of Object.keys(charts)) delete charts[key];
}

/** Describe the downsampling, so an averaged chart is never mistaken for raw. */
function describeSeries(series) {
    if (!series.bucketed) return '';
    const minutes = series.interval_seconds / 60;
    const interval = minutes >= 1440
        ? t('{n}-day', { n: minutes / 1440 })
        : minutes >= 60 ? t('{n}-hour', { n: minutes / 60 }) : t('{n}-minute', { n: minutes });
    return t('Averaged into {interval} intervals · shaded band shows the range within each',
        { interval });
}

export async function loadPeriod(period) {
    document.querySelectorAll('.period-btn[data-period]').forEach((btn) => {
        const active = btn.dataset.period === period;
        btn.classList.toggle('active', active);
        btn.setAttribute('aria-pressed', String(active));
    });

    if (!historyCache[period]) {
        const data = await fetchJSON(`/api/weather/history?period=${period}`);
        if (!data) return;
        historyCache[period] = data;
    }
    const series = historyCache[period];

    document.querySelectorAll('.chart-note').forEach((note) => {
        note.textContent = describeSeries(series);
    });

    if (!chartsAvailable() || !series.readings.length) return;

    destroyCharts();
    for (const metric of METRICS) {
        const canvas = document.getElementById(metric.canvas);
        if (!canvas) continue;
        charts[metric.canvas] = new Chart(canvas, makeChartConfig(metric, series, period));
        label(canvas, describeChart(metric, series, period));
    }
}

/* A canvas is a picture to everything that is not a pair of eyes: a screen
   reader finds an element with no role and no text and says nothing at all.
   These charts carry the shape of the data, which a summary cannot replace,
   but its range and period it can — and the alternative on offer was
   silence. Written here rather than in the template because only the drawing
   code knows which period is showing and what it spans. */
function label(canvas, text) {
    if (!canvas) return;
    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', text);
}

/** "Temperature over 7 Days: 3.1 to 21.4 °C", for a screen reader. */
function describeChart(metric, series, period) {
    const values = series.readings
        .map((r) => (r[`${metric.key}_min`] ?? r[metric.key]))
        .concat(series.readings.map((r) => r[`${metric.key}_max`] ?? r[metric.key]))
        .filter((v) => v !== null && v !== undefined && !Number.isNaN(v));
    if (!values.length) return t('{metric} chart, no readings', { metric: metric.label });

    const button = document.querySelector(`.period-btn[data-period="${period}"]`);
    return t('{metric} over {period}: {min} to {max} {unit}', {
        metric: metric.label,
        period: button ? button.textContent.trim() : period,
        min: formatNumber(Math.min(...values), metric.digits),
        max: formatNumber(Math.max(...values), metric.digits),
        unit: metric.unit,
    });
}

/** The series already fetched for a period, or undefined.
 *
 * The poller reads this so the page does not fetch the 24h series twice on
 * every load — once to draw the charts, once for the poller's first pass a
 * few milliseconds later, for the same 25 KB of the same readings.
 */
export const cachedSeries = (period) => historyCache[period];

/** Replace the cached 24h series and redraw if it is the visible period. */
export function refresh24h(series) {
    historyCache['24h'] = series;
    const active = document.querySelector('.period-btn.active');
    if (!active || active.dataset.period !== '24h' || !chartsAvailable()) return;

    for (const metric of METRICS) {
        const chart = charts[metric.canvas];
        if (!chart) continue;
        const config = makeChartConfig(metric, series, '24h');
        chart.data.datasets = config.data.datasets;
        chart.update();
        // The range moves with every new reading, so the description has to
        // move with it or it starts describing this morning.
        label(document.getElementById(metric.canvas),
              describeChart(metric, series, '24h'));
    }
}

/** Draw or update the small trend line on each current-conditions card. */
export function renderSparklines(readings) {
    if (!chartsAvailable() || !readings.length) return;

    for (const metric of METRICS) {
        const canvas = document.getElementById(metric.spark);
        if (!canvas) continue;

        const values = readings.map((r) => r[metric.key]);
        const min = Math.min(...values) - 1;
        const max = Math.max(...values) + 1;
        const existing = sparkCharts[metric.spark];

        if (existing) {
            // Update in place: destroying and recreating resizes the canvas.
            existing.data.labels = values.map((_, i) => i);
            existing.data.datasets[0].data = values;
            existing.options.scales.y.min = min;
            existing.options.scales.y.max = max;
            existing.update();
            continue;
        }

        sparkCharts[metric.spark] = new Chart(canvas, {
            type: 'line',
            data: {
                labels: values.map((_, i) => i),
                datasets: [{
                    data: values,
                    borderColor: metric.color.line,
                    borderWidth: 1.5,
                    fill: false,
                    pointRadius: 0,
                    tension: 0.3,
                }],
            },
            options: {
                // Sized from .sparkline-wrap, so the card can shrink and the
                // line follows a window resize or a device rotation.
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                plugins: { legend: { display: false }, tooltip: { enabled: false } },
                scales: { x: { display: false }, y: { display: false, min, max } },
                layout: { padding: 0 },
            },
        });
    }
}

/** Wire the period buttons and draw the default view. */
export function initCharts(defaultPeriod = '24h') {
    document.querySelectorAll('.period-btn[data-period]').forEach((btn) => {
        btn.addEventListener('click', () => loadPeriod(btn.dataset.period));
    });
    return loadPeriod(defaultPeriod);
}
