/* Shared formatting helpers and the theme-aware chart palette. */

export const COLORS = {
    temp: { line: '#e53e3e', bg: 'rgba(229,62,62,0.08)', band: 'rgba(229,62,62,0.13)' },
    hum:  { line: '#3182ce', bg: 'rgba(49,130,206,0.08)', band: 'rgba(49,130,206,0.13)' },
    pres: { line: '#38a169', bg: 'rgba(56,161,105,0.08)', band: 'rgba(56,161,105,0.13)' },
};

/** The three metrics, in the order they appear on the page. */
export const METRICS = [
    { key: 'temperature', canvas: 'chart-temp', spark: 'spark-temp', label: 'Temperature', color: COLORS.temp, digits: 1, unit: '°C' },
    { key: 'humidity',    canvas: 'chart-hum',  spark: 'spark-hum',  label: 'Humidity',    color: COLORS.hum,  digits: 0, unit: '%' },
    { key: 'pressure',    canvas: 'chart-pres', spark: 'spark-pres', label: 'Pressure',    color: COLORS.pres, digits: 0, unit: 'hPa' },
];

export const isDark = () =>
    window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;

/** Tick and grid colours that suit the active theme. */
export function chartStyle() {
    return {
        tickColor: isDark() ? '#a0aec0' : '#94a3b8',
        gridColor: isDark() ? '#4a5568' : '#e2e8f0',
    };
}

/** Chart.js is loaded from a CDN; without network the page must still work. */
export const chartsAvailable = () => typeof window.Chart !== 'undefined';

/** Parse a stored naive local timestamp ("YYYY-MM-DD HH:MM:SS") to epoch ms. */
export const parseTimestamp = (ts) => new Date(ts.replace(' ', 'T')).getTime();

/** Format a number with a fixed sign, e.g. "+1.4" / "-0.3". */
export const signed = (value, digits) =>
    `${value >= 0 ? '+' : ''}${value.toFixed(digits)}`;

/** Local YYYY-MM-DD, avoiding the UTC shift that toISOString() would apply. */
export const formatDateKey = (d) =>
    `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

export const MONTH_NAMES = [
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
];

export const MONTH_ABBR = [
    'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
];

/** Monday-first weekday labels, matching the calendar layout. */
export const DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

/** Fetch JSON, returning null instead of throwing on any failure. */
export async function fetchJSON(url) {
    try {
        const response = await fetch(url, { headers: { Accept: 'application/json' } });
        if (!response.ok) return null;
        return await response.json();
    } catch {
        return null;
    }
}
