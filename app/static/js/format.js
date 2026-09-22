/* Shared formatting helpers and the theme-aware chart palette.

   Language-dependent formatting lives in i18n.js; this re-exports the pieces
   the other modules reach for so their imports stay in one place. */

import { t, formatNumber, signed } from './i18n.js';

export { t, formatNumber, signed };
export {
    LANG, LOCALE, MONTH_NAMES, MONTH_ABBR, DAY_NAMES, dateFormat,
    formatDate, formatDateTime, formatRelative,
} from './i18n.js';

export const COLORS = {
    temp: { line: '#e53e3e', bg: 'rgba(229,62,62,0.08)', band: 'rgba(229,62,62,0.13)' },
    hum:  { line: '#3182ce', bg: 'rgba(49,130,206,0.08)', band: 'rgba(49,130,206,0.13)' },
    pres: { line: '#38a169', bg: 'rgba(56,161,105,0.08)', band: 'rgba(56,161,105,0.13)' },
};

/* The dew point rides on the temperature chart rather than getting one of
   its own: it is a temperature, in the same units, on the same axis, and
   the distance between the two lines is the reading — close means muggy,
   and a crossing is condensation. Blue because it is the moisture side of
   the pair, dashed because it is derived rather than measured. The server
   computes it (services.history_payload) so the formula has one home. */
export const DEW_POINT = {
    key: 'dew_point', label: t('Dew point'), color: COLORS.hum.line, dash: [5, 4],
};

/** The three metrics, in the order they appear on the page. */
export const METRICS = [
    { key: 'temperature', canvas: 'chart-temp', spark: 'spark-temp', label: t('Temperature'), color: COLORS.temp, digits: 1, unit: '°C', companion: DEW_POINT },
    { key: 'humidity',    canvas: 'chart-hum',  spark: 'spark-hum',  label: t('Humidity'),    color: COLORS.hum,  digits: 0, unit: '%' },
    { key: 'pressure',    canvas: 'chart-pres', spark: 'spark-pres', label: t('Pressure'),    color: COLORS.pres, digits: 0, unit: 'hPa' },
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

/** Local YYYY-MM-DD, avoiding the UTC shift that toISOString() would apply. */
export const formatDateKey = (d) =>
    `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

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
