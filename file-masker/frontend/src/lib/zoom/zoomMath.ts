/** Pure zoom math for the PDF preview zoom/pan engine. No React, no DOM. */

export const MIN_SCALE = 0.25;
export const MAX_SCALE = 5;
/** Multiplicative step used by the +/- buttons. */
export const ZOOM_STEP = 1.2;

export function clampScale(scale: number, min = MIN_SCALE, max = MAX_SCALE): number {
  if (Number.isNaN(scale)) return min;
  return Math.min(max, Math.max(min, scale));
}

export function computeFitWidthScale(
  containerWidth: number,
  contentNaturalWidth: number,
  min = MIN_SCALE,
  max = MAX_SCALE,
): number {
  if (!(contentNaturalWidth > 0) || !(containerWidth > 0)) return 1;
  return clampScale(containerWidth / contentNaturalWidth, min, max);
}

export function zoomStep(
  scale: number,
  direction: 1 | -1,
  min = MIN_SCALE,
  max = MAX_SCALE,
): number {
  const next = direction > 0 ? scale * ZOOM_STEP : scale / ZOOM_STEP;
  return clampScale(next, min, max);
}

export interface AnchoredScrollInput {
  oldScale: number;
  newScale: number;
  /** cursor offset from the scroll container's left edge, in client px */
  cursorX: number;
  /** cursor offset from the scroll container's top edge, in client px */
  cursorY: number;
  scrollLeft: number;
  scrollTop: number;
}

export interface ScrollOffset {
  scrollLeft: number;
  scrollTop: number;
}

export function computeAnchoredScroll(i: AnchoredScrollInput): ScrollOffset {
  const ratio = i.newScale / i.oldScale;
  return {
    scrollLeft: (i.scrollLeft + i.cursorX) * ratio - i.cursorX,
    scrollTop: (i.scrollTop + i.cursorY) * ratio - i.cursorY,
  };
}
