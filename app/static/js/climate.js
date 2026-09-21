/* Climate chart and the year switcher under it.

   The per-year monthly tables are rendered server-side, one hidden panel per
   year; this only toggles which is visible. Building them here as well would
   mean two implementations of the same table drifting apart. */

import { MONTH_ABBR, chartStyle, chartsAvailable, t, formatNumber, LOCALE } from './format.js';
import { createPager } from './pager.js';

const SERIES = [
    { key: 'temp_max', label: t('Max'),     color: '#e53e3e', width: 1.5, dash: [4, 3], radius: 3 },
    { key: 'temp_avg', label: t('Average'), color: '#38a169', width: 2.5, dash: [],     radius: 4 },
    { key: 'temp_min', label: t('Min'),     color: '#3182ce', width: 1.5, dash: [4, 3], radius: 3 },
];

function buildChart() {
    const dataEl = document.getElementById('climate-data');
    const canvas = document.getElementById('chart-climate');
    if (!dataEl || !canvas || !chartsAvailable()) return;

    const monthly = JSON.parse(dataEl.textContent);
    const style = chartStyle();

    new Chart(canvas, {
        type: 'line',
        data: {
            labels: monthly.map((m) => MONTH_ABBR[m.month - 1]),
            datasets: SERIES.map((series) => ({
                label: series.label,
                data: monthly.map((m) => m[series.key]),
                borderColor: series.color,
                backgroundColor: 'transparent',
                borderWidth: series.width,
                borderDash: series.dash,
                pointRadius: series.radius,
                pointHoverRadius: series.radius + 2,
                tension: 0.3,
            })),
        },
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
            },
            scales: {
                x: { ticks: { color: style.tickColor, font: { size: 10 } }, grid: { display: false } },
                y: {
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

/* The year pages are server-rendered side by side; this only turns them,
   with the same pager the temperature calendar uses. */
function initYearPager() {
    const track = document.getElementById('climate-track');
    if (!track || !track.children.length) return;

    const label = document.getElementById('climate-label');
    const years = [...track.children].map((page) => page.dataset.year);
    const showYear = (index) => {
        if (label && years[index]) label.textContent = years[index];
    };

    const pager = createPager({
        track,
        prev: document.getElementById('climate-prev'),
        next: document.getElementById('climate-next'),
        onChange: showYear,
    });

    // Open on the current year when there is one, else the most recent.
    const wanted = years.indexOf(track.dataset.default);
    pager.goTo(wanted >= 0 ? wanted : years.length - 1, false);
}

export function initClimate() {
    buildChart();
    initYearPager();
}
