/* Entry point: wire the dashboard once the markup is in place. */

import { initCharts } from './charts.js';
import { buildHeatmap } from './heatmap.js';
import { initClimate } from './climate.js';
import { startPolling } from './poll.js';
import { chartsAvailable, t } from './format.js';

/* Chart.js is served from this image, so this should not happen any more —
   it used to mean the CDN was unreachable. It is kept because the page still
   renders every value, record and calendar day without it, and an empty
   canvas with no explanation is worse than a sentence. */
function noteMissingCharts() {
    if (chartsAvailable()) return;
    document.querySelectorAll('.chart-fallback').forEach((el) => {
        el.textContent = t('The charting library did not load, so the graphs are missing.');
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
