import { describe, it, expect } from 'vitest';
import {
  clampScale,
  computeFitWidthScale,
  zoomStep,
  computeAnchoredScroll,
  MIN_SCALE,
  MAX_SCALE,
  ZOOM_STEP,
} from '../zoomMath';

describe('clampScale', () => {
  it('returns the value unchanged when inside the range', () => {
    expect(clampScale(1)).toBe(1);
  });
  it('clamps below the minimum up to min', () => {
    expect(clampScale(0.1)).toBe(MIN_SCALE);
  });
  it('clamps above the maximum down to max', () => {
    expect(clampScale(10)).toBe(MAX_SCALE);
  });
  it('returns min for NaN', () => {
    expect(clampScale(Number.NaN)).toBe(MIN_SCALE);
  });
  it('honours custom min/max args', () => {
    expect(clampScale(2, 0.5, 3)).toBe(2);
    expect(clampScale(5, 0.5, 3)).toBe(3);
  });
});

describe('computeFitWidthScale', () => {
  it('returns containerWidth / contentWidth when inside range', () => {
    expect(computeFitWidthScale(800, 780)).toBeCloseTo(800 / 780, 10);
  });
  it('scales down to 0.5 for a half-width container', () => {
    expect(computeFitWidthScale(390, 780)).toBeCloseTo(0.5, 10);
  });
  it('clamps the fit result to the minimum', () => {
    expect(computeFitWidthScale(100, 780)).toBe(MIN_SCALE);
  });
  it('returns 1 when content width is 0 (unmeasured)', () => {
    expect(computeFitWidthScale(800, 0)).toBe(1);
  });
  it('returns 1 when container width is 0 (unmeasured)', () => {
    expect(computeFitWidthScale(0, 780)).toBe(1);
  });
});

describe('zoomStep', () => {
  it('multiplies by ZOOM_STEP when zooming in', () => {
    expect(zoomStep(1, 1)).toBeCloseTo(ZOOM_STEP, 10);
  });
  it('divides by ZOOM_STEP when zooming out', () => {
    expect(zoomStep(1, -1)).toBeCloseTo(1 / ZOOM_STEP, 10);
  });
  it('clamps a zoom-in that would exceed max', () => {
    expect(zoomStep(4.5, 1)).toBe(MAX_SCALE);
  });
  it('clamps a zoom-out that would drop below min', () => {
    expect(zoomStep(0.3, -1)).toBe(MIN_SCALE);
  });
});

describe('computeAnchoredScroll', () => {
  it('keeps the cursor point fixed when zooming in from origin', () => {
    const r = computeAnchoredScroll({
      oldScale: 1, newScale: 2, cursorX: 100, cursorY: 0, scrollLeft: 0, scrollTop: 0,
    });
    expect(r.scrollLeft).toBeCloseTo(100, 10);
    expect(r.scrollTop).toBeCloseTo(0, 10);
  });
  it('is the inverse when zooming back out', () => {
    const r = computeAnchoredScroll({
      oldScale: 2, newScale: 1, cursorX: 100, cursorY: 0, scrollLeft: 100, scrollTop: 0,
    });
    expect(r.scrollLeft).toBeCloseTo(0, 10);
    expect(r.scrollTop).toBeCloseTo(0, 10);
  });
  it('accounts for a non-zero starting scroll and 2D cursor', () => {
    const r = computeAnchoredScroll({
      oldScale: 1, newScale: 1.5, cursorX: 50, cursorY: 30, scrollLeft: 20, scrollTop: 10,
    });
    expect(r.scrollLeft).toBeCloseTo(55, 10);
    expect(r.scrollTop).toBeCloseTo(30, 10);
  });
});
