/* Temperature calendar: one month per page in a scroll-snapping track. */

import { DAY_NAMES, MONTH_NAMES, formatDateKey, fetchJSON } from './format.js';

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
    const t = Math.max(SCALE_MIN, Math.min(SCALE_MAX, temp));
    let low = SCALE_MIN;
    for (const band of BANDS) {
        if (t < band.upTo || band.upTo === SCALE_MAX) {
            const span = band.upTo - low || 1;
            const ratio = Math.min((t - low) / span, 1);
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

const state = { track: null, months: [], index: 0 };

function makeCell(className) {
    const cell = document.createElement('div');
    cell.className = className;
    return cell;
}

function renderMonthPanel(year, month, dayMap, todayKey) {
    const panel = makeCell('heatmap-month');
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
            cell.title = `${key}\nMin: ${info.temp_min}°C  Max: ${info.temp_max}°C  Avg: ${info.temp_avg}°C`;
            const tempLabel = makeCell('cell-temp');
            tempLabel.textContent = `${Math.round(info.temp_avg)}°`;
            cell.appendChild(tempLabel);

            stats.count++;
            stats.sum += info.temp_avg;
            stats.min = stats.min === null ? info.temp_min : Math.min(stats.min, info.temp_min);
            stats.max = stats.max === null ? info.temp_max : Math.max(stats.max, info.temp_max);
        } else {
            cell.classList.add('heatmap-nodata');
            cell.title = `${key}\nNo data`;
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
            ['Avg', (stats.sum / stats.count).toFixed(1), 'avg'],
            ['Low', stats.min.toFixed(1), 'lo'],
            ['High', stats.max.toFixed(1), 'hi'],
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
        days.textContent = `${stats.count} day${stats.count === 1 ? '' : 's'} with data`;
        summary.appendChild(days);
    } else {
        summary.textContent = 'No readings this month';
    }
    panel.appendChild(summary);

    return panel;
}

function updateNav() {
    const month = state.months[state.index];
    const label = document.getElementById('heatmap-label');
    if (month && label) label.textContent = `${MONTH_NAMES[month.month]} ${month.year}`;

    const prev = document.getElementById('heatmap-prev');
    const next = document.getElementById('heatmap-next');
    if (prev) prev.disabled = state.index <= 0;
    if (next) next.disabled = state.index >= state.months.length - 1;
}

/**
 * The track holds every month side by side, so without this it would always
 * be as tall as the tallest one. Follow the panel in view, interpolating
 * mid-swipe so the card does not jump.
 */
function syncHeight() {
    const track = state.track;
    if (!track || !track.children.length || !track.clientWidth) return;

    const position = track.scrollLeft / track.clientWidth;
    const last = track.children.length - 1;
    const i = Math.max(0, Math.min(last, Math.floor(position)));
    const j = Math.max(0, Math.min(last, i + 1));
    const fraction = Math.max(0, Math.min(1, position - i));
    const height =
        track.children[i].offsetHeight * (1 - fraction) +
        track.children[j].offsetHeight * fraction;
    track.style.height = `${Math.round(height)}px`;
}

function goToMonth(index, smooth = true) {
    if (!state.track || !state.months.length) return;
    state.index = Math.max(0, Math.min(state.months.length - 1, index));
    state.track.scrollTo({
        left: state.index * state.track.clientWidth,
        behavior: smooth ? 'smooth' : 'auto',
    });
    updateNav();
    syncHeight();
}

function attachHandlers(track) {
    document.getElementById('heatmap-prev').onclick = () => goToMonth(state.index - 1);
    document.getElementById('heatmap-next').onclick = () => goToMonth(state.index + 1);

    let settleTimer;
    let pendingFrame = 0;
    track.addEventListener('scroll', () => {
        if (!pendingFrame) {
            pendingFrame = requestAnimationFrame(() => {
                pendingFrame = 0;
                syncHeight();
            });
        }
        clearTimeout(settleTimer);
        settleTimer = setTimeout(() => {
            if (!track.clientWidth) return;
            const i = Math.round(track.scrollLeft / track.clientWidth);
            if (i !== state.index) {
                state.index = Math.max(0, Math.min(state.months.length - 1, i));
                updateNav();
            }
        }, 80);
    });

    track.addEventListener('keydown', (event) => {
        if (event.key === 'ArrowLeft') {
            event.preventDefault();
            goToMonth(state.index - 1);
        } else if (event.key === 'ArrowRight') {
            event.preventDefault();
            goToMonth(state.index + 1);
        }
    });

    window.addEventListener('resize', () => goToMonth(state.index, false));

    // Cells are sized in vw, so panel heights change with the viewport.
    if (window.ResizeObserver) {
        const observer = new ResizeObserver(syncHeight);
        for (const panel of track.children) observer.observe(panel);
    }
}

export async function buildHeatmap() {
    const track = document.getElementById('heatmap-track');
    if (!track) return;
    state.track = track;

    const legend = document.querySelector('.heatmap-legend-bar');
    if (legend) legend.style.background = legendGradient();

    const daily = await fetchJSON('/api/weather/daily?months=24');
    if (!daily || !daily.length) {
        track.innerHTML = '';
        const note = makeCell('heatmap-note');
        note.textContent = 'Not enough data yet';
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

    goToMonth(state.months.length - 1, false);
    attachHandlers(track);
}
