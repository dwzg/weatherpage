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

let chart = null;
let byYear = {};

function datasets(months) {
    return SERIES.map((series) => ({
        label: series.label,
        data: months.map((m) => m[series.key]),
        borderColor: series.color,
        backgroundColor: 'transparent',
        borderWidth: series.width,
        borderDash: series.dash,
        pointRadius: series.radius,
        pointHoverRadius: series.radius + 2,
        tension: 0.3,
    }));
}

/**
 * One y-axis for every year.
 *
 * Chart.js would otherwise fit the axis to whichever year is showing, so a
 * mild year and a harsh one would draw the same shape and turning the page
 * would compare nothing. Bounds come from the whole archive.
 */
function sharedAxis() {
    const values = Object.values(byYear).flat().flatMap(
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
                    labels: { color: style.tickColor, font: { size: 10 }, boxWidth: 20, padding: 16 },
                },
                tooltip: {
                    // Hovering anywhere in a month's column gives all three
                    // values, which is how the months are read now that there
                    // is no table under the chart.
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

/** Redraw in place: destroying and recreating would resize the canvas. */
function showYear(months) {
    if (!chart) return;
    chart.data.datasets = datasets(months);
    chart.update();
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

    const label = document.getElementById('climate-label');
    const years = [...track.children].map((page) => page.dataset.year);

    const turn = (index) => {
        const year = years[index];
        if (!year) return;
        if (label) label.textContent = year;
        if (byYear[year]) showYear(byYear[year]);
    };

    const opening = years.indexOf(track.dataset.default);
    const start = opening >= 0 ? opening : years.length - 1;
    buildChart(byYear[years[start]] || []);

    const pager = createPager({
        track,
        prev: document.getElementById('climate-prev'),
        next: document.getElementById('climate-next'),
        onChange: turn,
    });
    pager.goTo(start, false);
}
