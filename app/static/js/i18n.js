/* The browser half of app/i18n.py.

   The server renders the page in one language and the poller then rewrites
   those same elements, so the two must agree. Rather than keep a second
   catalogue here, the render embeds the one it used in #i18n-data and this
   unpacks it: one table in app/i18n.py, two consumers.

   English is the message id, so its catalogue is empty and every lookup
   falls through to the key — which is exactly the English text. */

function payload() {
    const el = document.getElementById('i18n-data');
    if (!el) return {};
    try {
        return JSON.parse(el.textContent);
    } catch {
        return {};
    }
}

const DATA = payload();

export const LANG = DATA.lang || document.documentElement.lang || 'en';

/** Passed to Intl. en-GB, so English keeps the 24-hour clock it had. */
export const LOCALE = DATA.locale || 'en-GB';

const STRINGS = DATA.strings || {};
const DECIMAL = DATA.decimal || '.';
const THOUSANDS = DATA.thousands || ',';

export const MONTH_NAMES = DATA.months_long || [
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
];

export const MONTH_ABBR = DATA.months_short || [
    'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
];

/** Monday-first weekday labels, matching the calendar layout. */
export const DAY_NAMES = DATA.days_short || ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

/**
 * Translate, filling any {named} placeholders.
 *
 * An unknown key returns itself, which keeps the page readable if a string
 * is added here before it reaches the catalogue.
 */
export function t(text, fields) {
    const translated = STRINGS[text] ?? text;
    if (!fields) return translated;
    return translated.replace(/\{(\w+)\}/g, (match, name) =>
        (name in fields ? String(fields[name]) : match));
}

/**
 * Format a number the way the active language writes it — 22,5 in German.
 *
 * Built from the separators the server sent rather than from Intl, so that a
 * value the server rendered and the poller later rewrites cannot change shape
 * under the reader after sixty seconds.
 */
export function formatNumber(value, digits = 0, { sign = false, grouping = false } = {}) {
    if (value === null || value === undefined || Number.isNaN(value)) return '';
    const [whole, fraction] = Math.abs(value).toFixed(digits).split('.');
    const grouped = grouping ? whole.replace(/\B(?=(\d{3})+(?!\d))/g, THOUSANDS) : whole;
    const prefix = value < 0 ? '-' : sign ? '+' : '';
    return prefix + (fraction ? `${grouped}${DECIMAL}${fraction}` : grouped);
}

/** Format a number with an explicit sign, e.g. "+1,4" / "-0,3". */
export const signed = (value, digits) => formatNumber(value, digits, { sign: true });

const dateFormatters = new Map();

/** A cached Intl.DateTimeFormat for the page's locale. */
export function dateFormat(options) {
    const key = JSON.stringify(options);
    let formatter = dateFormatters.get(key);
    if (!formatter) {
        formatter = new Intl.DateTimeFormat(LOCALE, options);
        dateFormatters.set(key, formatter);
    }
    return formatter;
}
