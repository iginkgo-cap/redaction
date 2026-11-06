/**
 * Pure math for choosing the raster resolution of a PDF page (LOOK-6391). No React, no DOM.
 *
 * The zoom engine (`src/lib/zoom`) magnifies with CSS `transform: scale()`, which stretches an
 * already-painted bitmap — so a page rastered once at 1 bitmap px per CSS px goes soft the moment
 * it is enlarged, and is soft from the start on a HiDPI display. The fix is to re-raster at a
 * resolution that matches what the screen will actually show.
 *
 * Two constraints shape the answer:
 *
 * 1. **Quantize.** A trackpad pinch emits a continuous stream of scales. Tracking it exactly would
 *    re-raster on every frame, so the requirement is rounded *up* to a tier — never down, or the
 *    page would be under-sampled and blurry on purpose.
 * 2. **Cap.** `MAX_SCALE` is 5 and a HiDPI screen doubles that again, which would ask for a canvas
 *    wider than any browser will allocate. The tier is stepped down until the bitmap fits.
 */

/**
 * Render resolutions we are willing to raster at, ascending. Coarse on purpose: each step up is
 * roughly a doubling of pixel count, so the ladder trades a little sharpness between rungs for far
 * fewer re-rasters during a pinch.
 */
export const RENDER_SCALE_TIERS: readonly number[] = [1, 1.5, 2, 3, 4];

/**
 * Longest canvas edge we will ask a browser to allocate. Conservative on purpose: desktop Chrome
 * allows far more, but Safari (especially on iOS) refuses well below its documented maximum, and a
 * refused canvas renders as a blank page rather than a soft one.
 */
export const MAX_CANVAS_DIMENSION_PX = 4096;

export interface RenderScaleInput {
  /** Current zoom scale from `useZoomPan` — the CSS `transform: scale()` factor. */
  cssScale: number;
  /** `window.devicePixelRatio`; 1 on a standard display, 2+ on HiDPI. */
  devicePixelRatio: number;
  /** The page's unscaled CSS width, i.e. the width the canvas element occupies at `cssScale` 1. */
  pageWidthPx: number;
  /** The page's unscaled CSS height. */
  pageHeightPx: number;
}

/**
 * Bitmap pixels to paint per CSS pixel of page. A return of 2 means a page laid out at 780 CSS px
 * should be rastered into a 1560 px wide canvas.
 */
export function pickRenderScale(input: RenderScaleInput): number {
  const cssScale = positiveOr(input.cssScale, 1);
  const dpr = positiveOr(input.devicePixelRatio, 1);

  // A page we cannot measure is a page we cannot reason about a cap for. Paint it 1:1 rather than
  // guess at a multiplier that might ask for a canvas the browser refuses.
  if (!isPositive(input.pageWidthPx) || !isPositive(input.pageHeightPx)) return 1;

  const longestEdgePx = Math.max(input.pageWidthPx, input.pageHeightPx);
  const cap = MAX_CANVAS_DIMENSION_PX / longestEdgePx;

  // Round *up* to a tier: landing below the requirement would under-sample the page on purpose,
  // which is the very bug this module exists to fix.
  const required = cssScale * dpr;
  const wanted =
    RENDER_SCALE_TIERS.find((tier) => tier >= required) ??
    RENDER_SCALE_TIERS[RENDER_SCALE_TIERS.length - 1];

  if (wanted <= cap) return wanted;

  // Too big for the canvas budget — drop to the best tier that fits. If even the lowest tier
  // overflows (a poster-sized page), the raw cap is all that is left: slightly soft beats the
  // blank page a refused allocation would produce.
  const fits = [...RENDER_SCALE_TIERS].reverse().find((tier) => tier <= cap);
  return fits ?? cap;
}

function isPositive(value: number): boolean {
  return Number.isFinite(value) && value > 0;
}

function positiveOr(value: number, fallback: number): number {
  return isPositive(value) ? value : fallback;
}
