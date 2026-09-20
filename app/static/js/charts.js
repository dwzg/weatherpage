/* Main history charts and the per-card sparklines. */

import { METRICS, chartStyle, chartsAvailable, parseTimestamp, fetchJSON } from './format.js';

/** Axis formats per period. */
const TIME_FORMATS = {
    '24h': { unit: 'hour',  tooltip: 'HH:mm',     display: 'HH:mm' },
    '7d':  { unit: 'day',   tooltip: 'EEE HH:mm', display: 'EEE' },
    '30d': { unit: 'day',   tooltip: 'MMM d',     display: 'MMM d' },
    'all': { unit: 'month', tooltip: 'MMM yyyy',  display: "MMM ''yy" },
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
        const t = parseTimestamp(readings[i].timestamp);
        points.push({ x: t, y: readings[i][key] });

        if (i < readings.length - 1) {
            const next = parseTimestamp(readings[i + 1].timestamp);
            if (next - t > gapThresholdMs) points.push({ x: t + 1, y: NaN });
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
    return value.toFixed(decimals);
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
                label: 'Min',
                data: toBandData(series.readings, metric.key, 'min', gap),
                borderColor: 'transparent',
                backgroundColor: metric.color.band,
                pointRadius: 0,
                fill: '+1',
                tension: 0.3,
                spanGaps: false,
            },
            {
                label: 'Max',
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
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            plugins: {
                legend: { display: false },
                tooltip: {
                    // The band datasets would just repeat the average's tooltip.
                    filter: (item) => item.datasetIndex === datasets.length - 1,
                },
            },
            scales: {
                x: {
                    type: 'time',
                    time: {
                        unit: fmt.unit,
                        displayFormats: { [fmt.unit]: fmt.display },
                        tooltipFormat: fmt.tooltip,
                    },
                    ticks: {
                        maxTicksLimit: 8,
                        maxRotation: 0,
                        color: style.tickColor,
                        font: { size: 10 },
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
    const label = minutes >= 1440
        ? `${minutes / 1440}-day`
        : minutes >= 60 ? `${minutes / 60}-hour` : `${minutes}-minute`;
    return `Averaged into ${label} intervals · shaded band shows the range within each`;
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
        if (canvas) charts[metric.canvas] = new Chart(canvas, makeChartConfig(metric, series, period));
    }
}

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
