/* Temperature calendar: one month per page in a scroll-snapping track. */

import {
    DAY_NAMES, MONTH_NAMES, formatDateKey, fetchJSON, t, formatNumber, formatDate,
} from './format.js';
import { createPager } from './pager.js';

/* Fixed temperature→colour scale. Fixed rather than relative to the data so
   that the same colour always means the same temperature, across months and
   across years. */
const SCALE_MIN = -10;
const SCALE_MAX = 45;

const BANDS = [
    { upTo:  0, hue: [240, 220], sat: 65, light: 55 },  // freezing
    { upTo: 10, hue: [220, 170], sat: 60, light: 55 },  // cold
    { upTo: 18, hue: [170, 100], sat: 55, light: 55 },  // cool
    { upTo: 25, hue: [100,  45], sat: 60, light: 53 },  // warm
    { upTo: 30, hue: [45,   20], sat: 70, light: 50 },  // hot
    { upTo: 35, hue: [20,    5], sat: 75, light: 48 },  // very hot
    { upTo: SCALE_MAX, hue: [5, 0], sat: 80, light: 44 }, // extreme
];

export function tempToColor(temp) {
    const clamped = Math.max(SCALE_MIN, Math.min(SCALE_MAX, temp));
    let low = SCALE_MIN;
    for (const band of BANDS) {
        if (clamped < band.upTo || band.upTo === SCALE_MAX) {
            const span = band.upTo - low || 1;
            const ratio = Math.min((clamped - low) / span, 1);
            const hue = band.hue[0] + (band.hue[1] - band.hue[0]) * ratio;
            return `hsl(${Math.round(hue)},${band.sat}%,${band.light}%)`;
        }
        low = band.upTo;
    }
    return `hsl(0,80%,44%)`;
}

function legendGradient(steps = 40) {
    const stops = [];
    for (let i = 0; i <= steps; i++) {
        stops.push(tempToColor(SCALE_MIN + (i / steps) * (SCALE_MAX - SCALE_MIN)));
    }
    return `linear-gradient(to right, ${stops.join(', ')})`;
}

const state = { pager: null, months: [] };

function makeCell(className) {
    const cell = document.createElement('div');
    cell.className = className;
    return cell;
}

function renderMonthPanel(year, month, dayMap, todayKey) {
    const panel = makeCell('pager-page heatmap-month');
    const grid = makeCell('heatmap-grid');

    for (const name of DAY_NAMES) {
        const header = makeCell('heatmap-header');
        header.textContent = name;
        grid.appendChild(header);
    }

    // Monday-first offset of the 1st, and the month's length.
    const lead = (new Date(year, month, 1).getDay() + 6) % 7;
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    for (let i = 0; i < lead; i++) grid.appendChild(makeCell('heatmap-cell heatmap-empty'));

    const stats = { count: 0, sum: 0, min: null, max: null };

    for (let day = 1; day <= daysInMonth; day++) {
        const key = formatDateKey(new Date(year, month, day));
        const info = dayMap.get(key);
        const cell = makeCell('heatmap-cell');

        const dayLabel = makeCell('cell-day');
        dayLabel.textContent = String(day);
        cell.appendChild(dayLabel);

        if (info) {
            cell.style.backgroundColor = tempToColor(info.temp_avg);
            cell.title = [
                formatDate(key),
                `${t('Min')}: ${formatNumber(info.temp_min, 1)}°C`,
                `${t('Max')}: ${formatNumber(info.temp_max, 1)}°C`,
                `${t('Avg')}: ${formatNumber(info.temp_avg, 1)}°C`,
            ].join('\n');
            const tempLabel = makeCell('cell-temp');
            tempLabel.textContent = `${Math.round(info.temp_avg)}°`;
            cell.appendChild(tempLabel);

            stats.count++;
            stats.sum += info.temp_avg;
            stats.min = stats.min === null ? info.temp_min : Math.min(stats.min, info.temp_min);
            stats.max = stats.max === null ? info.temp_max : Math.max(stats.max, info.temp_max);
        } else {
            cell.classList.add('heatmap-nodata');
            cell.title = `${formatDate(key)}\n${t('No data')}`;
        }

        if (key === todayKey) cell.classList.add('is-today');
        grid.appendChild(cell);
    }

    // Pad only the final week, so the panel is as tall as the month needs.
    const trail = (7 - ((lead + daysInMonth) % 7)) % 7;
    for (let i = 0; i < trail; i++) grid.appendChild(makeCell('heatmap-cell heatmap-empty'));

    panel.appendChild(grid);

    const summary = makeCell('heatmap-month-summary');
    if (stats.count) {
        const parts = [
            [t('Avg'), formatNumber(stats.sum / stats.count, 1), 'avg'],
            [t('Low'), formatNumber(stats.min, 1), 'lo'],
            [t('High'), formatNumber(stats.max, 1), 'hi'],
        ];
        for (const [label, value, cls] of parts) {
            const span = document.createElement('span');
            span.append(`${label} `);
            const strong = document.createElement('span');
            strong.className = cls;
            strong.textContent = `${value}°C`;
            span.appendChild(strong);
            summary.appendChild(span);
        }
        const days = document.createElement('span');
        days.textContent = t(stats.count === 1 ? '{count} day with data' : '{count} days with data',
            { count: stats.count });
        summary.appendChild(days);
    } else {
        summary.textContent = t('No readings this month');
    }
    panel.appendChild(summary);

    return panel;
}

/** The month a page shows, in the nav label above the track. */
function showMonth(index) {
    const month = state.months[index];
    const label = document.getElementById('heatmap-label');
    if (month && label) label.textContent = `${MONTH_NAMES[month.month]} ${month.year}`;
}

export async function buildHeatmap() {
    const track = document.getElementById('heatmap-track');
    if (!track) return;

    const legend = document.querySelector('.heatmap-legend-bar');
    if (legend) legend.style.background = legendGradient();

    const daily = await fetchJSON('/api/weather/daily?months=24');
    if (!daily || !daily.length) {
        track.innerHTML = '';
        const note = makeCell('heatmap-note');
        note.textContent = t('Not enough data yet');
        track.appendChild(note);
        return;
    }

    const dayMap = new Map(daily.map((d) => [d.day, d]));
    const first = new Date(`${daily[0].day}T00:00:00`);
    const last = new Date(`${daily[daily.length - 1].day}T00:00:00`);
    const today = new Date();
    const todayKey = formatDateKey(today);

    // Run the calendar to the current month even if today has no reading yet.
    const lastMonth = new Date(Math.max(
        new Date(last.getFullYear(), last.getMonth(), 1).getTime(),
        new Date(today.getFullYear(), today.getMonth(), 1).getTime(),
    ));

    track.innerHTML = '';
    state.months = [];
    const cursor = new Date(first.getFullYear(), first.getMonth(), 1);
    while (cursor <= lastMonth) {
        track.appendChild(renderMonthPanel(cursor.getFullYear(), cursor.getMonth(), dayMap, todayKey));
        state.months.push({ year: cursor.getFullYear(), month: cursor.getMonth() });
        cursor.setMonth(cursor.getMonth() + 1);
    }

    state.pager = createPager({
        track,
        prev: document.getElementById('heatmap-prev'),
        next: document.getElementById('heatmap-next'),
        onChange: showMonth,
    });
    state.pager.goTo(state.months.length - 1, false);
}
