import { describe, it, expect } from 'vitest';
import { pickRenderScale, RENDER_SCALE_TIERS, MAX_CANVAS_DIMENSION_PX } from '../renderScale';

/** US-Letter-ish page as QuickForm lays it out: 780 CSS px wide. */
const LETTER = { pageWidthPx: 780, pageHeightPx: 1010 };

const at = (cssScale: number, devicePixelRatio = 1, page = LETTER) =>
  pickRenderScale({ cssScale, devicePixelRatio, ...page });

describe('pickRenderScale', () => {
  describe('matching the resolution the screen will show', () => {
    it('returns 1 when neither zoom nor the display asks for more pixels', () => {
      expect(at(1, 1)).toBe(1);
    });

    it('multiplies the CSS scale by the device pixel ratio', () => {
      expect(at(1, 2)).toBe(2);
      expect(at(2, 1)).toBe(2);
    });

    it('rounds a fractional requirement up to the next tier, never down', () => {
      // 1.2 x 2 = 2.4 device px per CSS px. Tier 2 would under-sample on purpose.
      expect(at(1.2, 2)).toBe(3);
    });

    it('returns the exact tier when the requirement lands on one', () => {
      expect(at(1.5, 1)).toBe(1.5);
      expect(at(1.5, 2)).toBe(3);
    });
  });

  describe('quantizing so a pinch does not re-raster every frame', () => {
    it('returns one identical tier across a range of CSS scales', () => {
      const tiers = [2.1, 2.3, 2.5, 2.7, 2.9, 3].map((s) => at(s, 1));
      expect(new Set(tiers)).toEqual(new Set([3]));
    });

    it('changes tier only once the requirement crosses the next rung', () => {
      expect(at(2, 1)).toBe(2);
      expect(at(2.0001, 1)).toBe(3);
    });
  });

  describe('capping so the browser can actually allocate the canvas', () => {
    it('clamps to the highest tier when zoom asks for more than the ladder covers', () => {
      const highest = RENDER_SCALE_TIERS[RENDER_SCALE_TIERS.length - 1];
      // 5 x 2 = 10, far past the ladder; a Letter page still fits at the top tier.
      expect(at(5, 2)).toBe(highest);
      expect(LETTER.pageHeightPx * highest).toBeLessThanOrEqual(MAX_CANVAS_DIMENSION_PX);
    });

    it('steps down to the largest tier whose bitmap width fits the dimension cap', () => {
      // 4096 / 1240 = 3.30, so tier 4 overflows and tier 3 is the best that fits.
      expect(at(5, 2, { pageWidthPx: 1240, pageHeightPx: 1000 })).toBe(3);
    });

    it('caps on page height too, so a tall page is not allocated past the limit', () => {
      // 4096 / 1400 = 2.92 — width alone would have allowed tier 4.
      expect(at(5, 2, { pageWidthPx: 780, pageHeightPx: 1400 })).toBe(2);
    });

    it('falls back to the raw dimension cap when even the lowest tier overflows', () => {
      const page = { pageWidthPx: 5000, pageHeightPx: 1000 };
      expect(at(5, 2, page)).toBeCloseTo(MAX_CANVAS_DIMENSION_PX / 5000, 10);
      expect(at(5, 2, page)).toBeLessThan(RENDER_SCALE_TIERS[0]);
    });

    it('never returns a scale that would exceed the cap on either axis', () => {
      const pages = [
        LETTER,
        { pageWidthPx: 1240, pageHeightPx: 1000 },
        { pageWidthPx: 780, pageHeightPx: 1400 },
        { pageWidthPx: 5000, pageHeightPx: 1000 },
        { pageWidthPx: 300, pageHeightPx: 300 },
      ];
      for (const page of pages) {
        for (const cssScale of [0.25, 1, 1.7, 3, 5]) {
          for (const dpr of [1, 2, 3]) {
            const scale = at(cssScale, dpr, page);
            const longest = Math.max(page.pageWidthPx, page.pageHeightPx) * scale;
            expect(longest).toBeLessThanOrEqual(MAX_CANVAS_DIMENSION_PX);
          }
        }
      }
    });
  });

  describe('degenerate inputs', () => {
    it('returns 1 for a non-finite CSS scale', () => {
      expect(at(Number.NaN, 1)).toBe(1);
      expect(at(Number.POSITIVE_INFINITY, 1)).toBe(1);
    });

    it('treats a non-finite device pixel ratio as 1', () => {
      expect(at(2, Number.NaN)).toBe(2);
    });

    it('treats a zero or negative device pixel ratio as 1', () => {
      expect(at(2, 0)).toBe(2);
      expect(at(2, -2)).toBe(2);
    });

    it('returns 1 when the page has no usable width or height', () => {
      expect(at(5, 2, { pageWidthPx: 0, pageHeightPx: 1010 })).toBe(1);
      expect(at(5, 2, { pageWidthPx: 780, pageHeightPx: 0 })).toBe(1);
      expect(at(5, 2, { pageWidthPx: Number.NaN, pageHeightPx: 1010 })).toBe(1);
    });
  });
});
