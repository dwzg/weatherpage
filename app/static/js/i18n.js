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

const DATE_FORMAT = DATA.date_format || '{d} {mon} {y}';
const DATETIME_FORMAT = DATA.datetime_format || '{date}, {time}';

const fill = (template, fields) =>
    template.replace(/\{(\w+)\}/g, (match, name) =>
        (name in fields ? String(fields[name]) : match));

/**
 * A stored "YYYY-MM-DD HH:MM:SS" as a date, written the way this language
 * writes it. The twin of i18n.format_date.
 *
 * Deliberately string arithmetic rather than Intl on a parsed Date: the
 * stored timestamp is a naive local wall clock, and handing it to Date()
 * would have the browser read it in *its* timezone. Reformatting the string
 * cannot be wrong that way. It also keeps the poller's output identical in
 * shape to what the server rendered, which is the rule the numbers follow.
 */
export function formatDate(timestamp) {
    if (!timestamp || timestamp.length < 10) return timestamp || '';
    const month = Number(timestamp.slice(5, 7));
    const mon = MONTH_ABBR[month - 1];
    if (!mon) return timestamp;
    return fill(DATE_FORMAT, {
        d: String(Number(timestamp.slice(8, 10))),   // "1 Sep 2026"
        dd: timestamp.slice(8, 10),                  // "01.09.2026"
        m: timestamp.slice(5, 7),
        mon,
        y: timestamp.slice(0, 4),
    });
}

/** The same, with the 24-hour clock. The twin of i18n.format_datetime. */
export function formatDateTime(timestamp) {
    if (!timestamp || timestamp.length < 16) return formatDate(timestamp);
    return fill(DATETIME_FORMAT, {
        date: formatDate(timestamp),
        time: timestamp.slice(11, 16),
    });
}

const JUST_NOW_SECONDS = 60;
const MAX_RELATIVE_SECONDS = 86400;

/**
 * How long ago, in words, or null when a date would serve better.
 * The twin of i18n.format_relative — same thresholds, same wording.
 *
 * The age comes from the server (status.age_seconds), not from subtracting
 * the stored timestamp here: it is a local wall clock, and a reader in
 * another timezone would otherwise see a reading from a minute ago reported
 * as an hour old.
 */
export function formatRelative(seconds) {
    if (seconds === null || seconds === undefined) return null;
    if (seconds < JUST_NOW_SECONDS) return t('just now');
    if (seconds >= MAX_RELATIVE_SECONDS) return null;
    if (seconds >= 3600) {
        const hours = Math.floor(seconds / 3600);
        return t(hours === 1 ? '{n} hour ago' : '{n} hours ago', { n: hours });
    }
    const minutes = Math.floor(seconds / 60);
    return t(minutes === 1 ? '{n} minute ago' : '{n} minutes ago', { n: minutes });
}

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
