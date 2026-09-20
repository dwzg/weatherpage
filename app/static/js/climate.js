/* Climate chart and the year switcher under it.

   The per-year monthly tables are rendered server-side, one hidden panel per
   year; this only toggles which is visible. Building them here as well would
   mean two implementations of the same table drifting apart. */

import { MONTH_ABBR, chartStyle, chartsAvailable } from './format.js';

const SERIES = [
    { key: 'temp_max', label: 'Max',     color: '#e53e3e', width: 1.5, dash: [4, 3], radius: 3 },
    { key: 'temp_avg', label: 'Average', color: '#38a169', width: 2.5, dash: [],     radius: 4 },
    { key: 'temp_min', label: 'Min',     color: '#3182ce', width: 1.5, dash: [4, 3], radius: 3 },
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
                        callback: (v) => `${v.toFixed(0)}°`,
                    },
                    grid: { color: style.gridColor },
                },
            },
            interaction: { intersect: false, mode: 'index' },
        },
    });
}

function showYear(year) {
    document.querySelectorAll('.climate-yr-btn').forEach((btn) => {
        const active = btn.dataset.year === year;
        btn.classList.toggle('active', active);
        btn.setAttribute('aria-pressed', String(active));
    });
    document.querySelectorAll('.year-panel').forEach((panel) => {
        panel.hidden = panel.dataset.year !== year;
    });
}

function initYearSwitcher() {
    const selector = document.getElementById('climate-year-selector');
    if (!selector) return;

    selector.querySelectorAll('.climate-yr-btn').forEach((btn) => {
        btn.addEventListener('click', () => showYear(btn.dataset.year));
    });
    if (selector.dataset.default) showYear(selector.dataset.default);
}

export function initClimate() {
    buildChart();
    initYearSwitcher();
}
