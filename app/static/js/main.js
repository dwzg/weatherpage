/* Entry point: wire the dashboard once the markup is in place. */

import { initCharts } from './charts.js';
import { buildHeatmap } from './heatmap.js';
import { initClimate } from './climate.js';
import { startPolling } from './poll.js';
import { chartsAvailable, t } from './format.js';

/* Chart.js comes from a CDN, so an offline page still renders its values and
   tables — only the canvases stay empty. Say so rather than leaving blanks. */
function noteMissingCharts() {
    if (chartsAvailable()) return;
    document.querySelectorAll('.chart-fallback').forEach((el) => {
        el.textContent = t('Charts need network access to load the charting library.');
    });
}

async function init() {
    noteMissingCharts();
    await initCharts('24h');
    buildHeatmap();
    initClimate();
    startPolling();
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}
