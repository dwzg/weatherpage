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

/* Three of these lightnesses are one to five points off the value they were
   designed at, and the reason is arithmetic rather than taste. No ink reaches
   4.5:1 against a background whose relative luminance falls between 0.183 and
   0.209 — above that white fails, below it black does — and this ramp crossed
   that window twice, around 0 °C and around 32 °C. The nudges step the two
   offending bands around it; the hues, which are what actually carry
   temperature here, are untouched. See cellInk() below. */
const BANDS = [
    { upTo:  0, hue: [240, 220], sat: 65, light: 54 },  // freezing  (was 55)
    { upTo: 10, hue: [220, 170], sat: 60, light: 58 },  // cold      (was 55)
    { upTo: 18, hue: [170, 100], sat: 55, light: 55 },  // cool
    { upTo: 25, hue: [100,  45], sat: 60, light: 53 },  // warm
    { upTo: 30, hue: [45,   20], sat: 70, light: 50 },  // hot
    { upTo: 35, hue: [20,    5], sat: 75, light: 43 },  // very hot  (was 48)
    { upTo: SCALE_MAX, hue: [5, 0], sat: 80, light: 44 }, // extreme
];

/** Where a temperature sits on the scale, as HSL components. */
function tempToHsl(temp) {
    const clamped = Math.max(SCALE_MIN, Math.min(SCALE_MAX, temp));
    let low = SCALE_MIN;
    for (const band of BANDS) {
        if (clamped < band.upTo || band.upTo === SCALE_MAX) {
            const span = band.upTo - low || 1;
            const ratio = Math.min((clamped - low) / span, 1);
            const hue = band.hue[0] + (band.hue[1] - band.hue[0]) * ratio;
            return { h: Math.round(hue), s: band.sat, l: band.light };
        }
        low = band.upTo;
    }
    return { h: 0, s: 80, l: 44 };
}

export function tempToColor(temp) {
    const { h, s, l } = tempToHsl(temp);
    return `hsl(${h},${s}%,${l}%)`;
}

/* ── Legible text on a scale that ignores the theme ──────────────────────
 *
 * The cell colours mean a temperature, so they are the same in light mode
 * and dark. The day number on top of them was not: it came from
 * --cell-text, which flips with the theme, so dark mode painted white at
 * 70% over lime green. Measured across the scale, nothing reached AA in
 * either mode, and dark mode was worst exactly where the calendar is
 * greenest:
 *
 *     cell     light (black@.6)   dark (white@.7)
 *     -10 °C       2.24               4.29
 *       5 °C       4.05               1.95
 *      20 °C       4.63               1.55
 *      45 °C       2.62               3.39
 *
 * A single flat ink cannot fix it either — solid black is 12:1 at 22 °C and
 * 3.7:1 at both ends. The colour has to come from the cell, which is the one
 * thing that knows how light it is.
 */
const INK_DARK = '#10151c';
const INK_LIGHT = '#ffffff';

/** sRGB channel to its linear value, per WCAG. */
const linearise = (channel) =>
    channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;

function hslLuminance({ h, s, l }) {
    const sat = s / 100;
    const light = l / 100;
    const c = (1 - Math.abs(2 * light - 1)) * sat;
    const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
    const m = light - c / 2;
    const [r, g, b] = (
        h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x]
            : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x]
    ).map((v) => linearise(v + m));
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

const contrast = (a, b) =>
    (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);

const INK_LUMINANCE = {
    [INK_DARK]: hslLuminance({ h: 213, s: 28, l: 9 }),
    [INK_LIGHT]: 1,
};

/**
 * The ink for a cell, chosen from that cell's own colour.
 *
 * Whichever of the two inks contrasts better with the background it is going
 * on, which clears AA across the whole scale and is the same answer in both
 * themes — as it should be, since the cell is.
 */
export function cellInk(temp) {
    const background = hslLuminance(tempToHsl(temp));
    return contrast(background, INK_LUMINANCE[INK_DARK])
        >= contrast(background, INK_LUMINANCE[INK_LIGHT])
        ? INK_DARK
        : INK_LIGHT;
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

    /* Tapping a day has to lead somewhere. The numbers used to live only in
       a title attribute, which on a phone — the likeliest way anyone reads a
       balcony weather page — is unreachable: touch has no hover, so the
       calendar was 600 coloured squares and no way to ask what any of them
       meant. This line is where a tap puts them. */
    const detail = makeCell('heatmap-detail');
    detail.setAttribute('aria-live', 'polite');

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
            // Chosen from this cell's own colour, not from the theme.
            cell.style.color = cellInk(info.temp_avg);
            const parts = [
                `${t('Min')}: ${formatNumber(info.temp_min, 1)}°C`,
                `${t('Max')}: ${formatNumber(info.temp_max, 1)}°C`,
                `${t('Avg')}: ${formatNumber(info.temp_avg, 1)}°C`,
            ];
            cell.title = [formatDate(key), ...parts].join('\n');
            /* The same sentence a hover gives, for a screen reader reading
               the grid and for the line below when the day is picked. */
            cell.setAttribute('aria-label', `${formatDate(key)}, ${parts.join(', ')}`);
            cell.dataset.detail = `${formatDate(key)} · ${parts.join(' · ')}`;
            cell.classList.add('is-pickable');
            const select = () => selectDay(panel, cell);
            cell.addEventListener('click', select);
            cell.addEventListener('focus', select);

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
    panel.appendChild(detail);
    panel.appendChild(summary);

    return panel;
}

/** Show one day's numbers under its own month, and mark it as the picked one. */
function selectDay(panel, cell) {
    const detail = panel.querySelector('.heatmap-detail');
    if (!detail) return;
    for (const other of panel.querySelectorAll('.heatmap-cell.is-picked')) {
        other.classList.remove('is-picked');
    }
    cell.classList.add('is-picked');
    detail.textContent = cell.dataset.detail || '';
}

/* Only the month on screen is in the tab order.
 *
 * Two years of calendar is some 750 day cells; making all of them tab stops
 * would bury everything below the card behind 750 presses. Moving the stops
 * with the pager keeps it to a month, which is the month a reader can
 * actually see. Arrow keys are left to the pager — they turn the month, as
 * they always have — so focus is moved off a page that scrolls away rather
 * than being left on a cell nobody can see.
 */
function updateTabStops(index) {
    const track = document.getElementById('heatmap-track');
    if (!track) return;
    const active = track.children[index];
    for (const page of track.children) {
        const inView = page === active;
        for (const cell of page.querySelectorAll('.heatmap-cell.is-pickable')) {
            cell.tabIndex = inView ? 0 : -1;
        }
    }
    if (active && document.activeElement instanceof HTMLElement
        && !active.contains(document.activeElement)
        && track.contains(document.activeElement)) {
        track.focus();
    }
}

/** The month a page shows, in the nav label above the track. */
function showMonth(index) {
    const month = state.months[index];
    const label = document.getElementById('heatmap-label');
    if (month && label) label.textContent = `${MONTH_NAMES[month.month]} ${month.year}`;
    updateTabStops(index);
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
