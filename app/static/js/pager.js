/* A horizontal scroll-snap pager: one page in view at a time, with arrows,
   swipe, arrow keys and a height that follows the page you are looking at.

   The temperature calendar and the climate year both page this way. They had
   no business being two implementations — the height interpolation in
   particular is fiddly enough that a second copy would drift — so the
   behaviour lives here and each caller supplies its own pages and label. */

/**
 * @param {object} options
 * @param {HTMLElement} options.track   the scroll container; its children are the pages
 * @param {HTMLElement} [options.prev]  button to step back
 * @param {HTMLElement} [options.next]  button to step forward
 * @param {(index: number) => void} [options.onChange]  called when the page in view changes
 * @returns {{goTo: Function, sync: Function, index: () => number}}
 */
export function createPager({ track, prev, next, onChange }) {
    let index = 0;
    const last = () => track.children.length - 1;
    const clamp = (i) => Math.max(0, Math.min(last(), i));

    /**
     * The track holds every page side by side, so without this it would
     * always be as tall as the tallest one. Follow the page in view,
     * interpolating mid-swipe so the card does not jump.
     */
    function sync() {
        if (!track.children.length || !track.clientWidth) return;
        const position = track.scrollLeft / track.clientWidth;
        const i = clamp(Math.floor(position));
        const j = clamp(i + 1);
        const fraction = Math.max(0, Math.min(1, position - i));
        const height =
            track.children[i].offsetHeight * (1 - fraction) +
            track.children[j].offsetHeight * fraction;
        track.style.height = `${Math.round(height)}px`;
    }

    function updateNav() {
        if (prev) prev.disabled = index <= 0;
        if (next) next.disabled = index >= last();
        if (onChange) onChange(index);
    }

    function goTo(to, smooth = true) {
        if (!track.children.length) return;
        index = clamp(to);
        track.scrollTo({
            left: index * track.clientWidth,
            behavior: smooth ? 'smooth' : 'auto',
        });
        updateNav();
        sync();
    }

    if (prev) prev.onclick = () => goTo(index - 1);
    if (next) next.onclick = () => goTo(index + 1);

    let settleTimer;
    let pendingFrame = 0;
    track.addEventListener('scroll', () => {
        if (!pendingFrame) {
            pendingFrame = requestAnimationFrame(() => {
                pendingFrame = 0;
                sync();
            });
        }
        clearTimeout(settleTimer);
        settleTimer = setTimeout(() => {
            if (!track.clientWidth) return;
            const i = Math.round(track.scrollLeft / track.clientWidth);
            if (i !== index) {
                index = clamp(i);
                updateNav();
            }
        }, 80);
    });

    track.addEventListener('keydown', (event) => {
        if (event.key === 'ArrowLeft') {
            event.preventDefault();
            goTo(index - 1);
        } else if (event.key === 'ArrowRight') {
            event.preventDefault();
            goTo(index + 1);
        }
    });

    window.addEventListener('resize', () => goTo(index, false));

    // Pages are sized in vw, so their heights change with the viewport.
    if (window.ResizeObserver) {
        const observer = new ResizeObserver(sync);
        for (const page of track.children) observer.observe(page);
    }

    return { goTo, sync, index: () => index };
}
