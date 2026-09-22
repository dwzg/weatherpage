/* The climate card: twelve months of one year, with the year on a pager.

   The chart and the pager show the same year — turning the page redraws the
   chart rather than revealing a second copy of the numbers. The months are
   read off the chart; the page under it carries the one thing hovering a
   month cannot tell you, which is the year as a whole.

   Every year's data is embedded once by the render, so switching years costs
   no request. */

import { MONTH_ABBR, chartStyle, chartsAvailable, t, formatNumber, LOCALE } from './format.js';
import { createPager } from './pager.js';

const SERIES = [
    { key: 'temp_max', label: t('Max'),     color: '#e53e3e', width: 1.5, dash: [4, 3], radius: 3 },
    { key: 'temp_avg', label: t('Average'), color: '#38a169', width: 2.5, dash: [],     radius: 4 },
    { key: 'temp_min', label: t('Min'),     color: '#3182ce', width: 1.5, dash: [4, 3], radius: 3 },
];

/* Headroom above and below the warmest and coldest month in the archive. */
const AXIS_PADDING_C = 2;

/* The archive's own range for each month, behind whichever year is showing:
   the year's line inside it is an ordinary year, a line at its edge is the
   record. Grey because it is context rather than a fourth measurement, and
   drawn first because Chart.js paints datasets in order. */
const NORM_FILL = 'rgba(113, 128, 150, 0.16)';

let chart = null;
let byYear = {};
let norm = null;

function bandDatasets() {
    if (!norm) return [];
    const empty = { borderColor: 'transparent', pointRadius: 0, tension: 0.3 };
    return [
        {
            ...empty,
            label: t('All years, coldest to warmest'),
            data: norm.map((m) => m.temp_min),
            backgroundColor: NORM_FILL,
            fill: '+1',
        },
        {
            ...empty,
            // No label: one legend entry describes the pair, and the filter
            // below drops this one.
            data: norm.map((m) => m.temp_max),
            fill: false,
        },
    ];
}

function datasets(months) {
    return [...bandDatasets(), ...SERIES.map((series) => ({
        label: series.label,
        data: months.map((m) => m[series.key]),
        borderColor: series.color,
        backgroundColor: 'transparent',
        borderWidth: series.width,
        borderDash: series.dash,
        pointRadius: series.radius,
        pointHoverRadius: series.radius + 2,
        tension: 0.3,
    }))];
}

/**
 * One y-axis for every year.
 *
 * Chart.js would otherwise fit the axis to whichever year is showing, so a
 * mild year and a harsh one would draw the same shape and turning the page
 * would compare nothing. Bounds come from the whole archive.
 */
function sharedAxis() {
    const values = Object.values(byYear).flat().concat(norm || []).flatMap(
        (m) => [m.temp_min, m.temp_max]).filter((v) => v !== null && v !== undefined);
    if (!values.length) return {};
    return {
        suggestedMin: Math.floor(Math.min(...values)) - AXIS_PADDING_C,
        suggestedMax: Math.ceil(Math.max(...values)) + AXIS_PADDING_C,
    };
}

function buildChart(months) {
    const canvas = document.getElementById('chart-climate');
    if (!canvas || !chartsAvailable()) return;
    const style = chartStyle();
    const axis = sharedAxis();

    chart = new Chart(canvas, {
        type: 'line',
        data: { labels: MONTH_ABBR, datasets: datasets(months) },
        options: {
            locale: LOCALE,
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            plugins: {
                legend: {
                    position: 'bottom',
                    labels: {
                        color: style.tickColor,
                        font: { size: 10 },
                        boxWidth: 20,
                        padding: 16,
                        // The band's upper edge carries no label; its lower
                        // edge speaks for both.
                        filter: (item) => Boolean(item.text),
                    },
                },
                tooltip: {
                    // Hovering anywhere in a month's column gives all three
                    // values, which is how the months are read now that there
                    // is no table under the chart. The band is context and
                    // stays out of it — it would add two rows saying the
                    // same thing about every month of every year.
                    filter: (item) => item.datasetIndex >= (norm ? 2 : 0),
                    callbacks: {
                        label: (item) =>
                            `${item.dataset.label}: ${formatNumber(item.parsed.y, 1)} °C`,
                    },
                },
            },
            scales: {
                x: { ticks: { color: style.tickColor, font: { size: 10 } }, grid: { display: false } },
                y: {
                    ...axis,
                    ticks: {
                        color: style.tickColor,
                        font: { size: 10 },
                        callback: (v) => `${formatNumber(v, 0)}°`,
                    },
                    grid: { color: style.gridColor },
                },
            },
            interaction: { intersect: false, mode: 'index' },
        },
    });
}

/* What this chart says, for a screen reader, which finds a bare canvas and
   reads nothing. The year is in it because the pager changes which year is
   drawn without changing anything else on the page. */
function label(year, months) {
    const canvas = document.getElementById('chart-climate');
    // Nothing was drawn, so there is nothing to describe; main.js hides the
    // empty canvas and the fallback sentence speaks for it instead.
    if (!canvas || !chartsAvailable()) return;
    const values = months.flatMap((m) => [m.temp_min, m.temp_max])
        .filter((v) => v !== null && v !== undefined);
    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', values.length
        ? t('Monthly average, minimum and maximum temperature for {year}: {min} to {max} °C',
            { year, min: formatNumber(Math.min(...values), 1),
              max: formatNumber(Math.max(...values), 1) })
        : t('Monthly temperatures for {year}, no readings', { year }));
}

/** Redraw in place: destroying and recreating would resize the canvas. */
function showYear(year, months) {
    if (!chart) return;
    chart.data.datasets = datasets(months);
    chart.update();
    label(year, months);
}

export function initClimate() {
    const dataEl = document.getElementById('climate-data');
    const track = document.getElementById('climate-track');
    if (!dataEl || !track || !track.children.length) return;

    try {
        byYear = JSON.parse(dataEl.textContent);
    } catch {
        return;
    }

    const normEl = document.getElementById('climate-norm');
    if (normEl) {
        try {
            norm = JSON.parse(normEl.textContent);
        } catch {
            norm = null;
        }
    }

    const labelEl = document.getElementById('climate-label');
    const years = [...track.children].map((page) => page.dataset.year);

    const turn = (index) => {
        const year = years[index];
        if (!year) return;
        if (labelEl) labelEl.textContent = year;
        if (byYear[year]) showYear(year, byYear[year]);
    };

    const opening = years.indexOf(track.dataset.default);
    const start = opening >= 0 ? opening : years.length - 1;
    buildChart(byYear[years[start]] || []);
    label(years[start], byYear[years[start]] || []);

    const pager = createPager({
        track,
        prev: document.getElementById('climate-prev'),
        next: document.getElementById('climate-next'),
        onChange: turn,
    });
    pager.goTo(start, false);
}
