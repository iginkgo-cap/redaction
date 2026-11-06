import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderComponent } from '../../../testing/renderComponent';

/**
 * Interleaving tests for the LOOK-6391 re-raster loop.
 *
 * The fake render task below mirrors pdf.js's own guard: `InternalRenderTask.initializeGraphics`
 * throws "Cannot use the same canvas during multiple render() operations" when a canvas is already
 * in `#canvasInUse`, and `cancel()` removes it from that set *synchronously*. Reproducing that
 * exact shape is the point — a double that merely counts calls could not tell us whether the real
 * error would fire.
 */

interface FakeTask {
  promise: Promise<void>;
  cancel: () => void;
  finish: () => void;
  canvas: HTMLCanvasElement;
  pageNumber: number;
  settled: boolean;
  overlay: boolean;
}

const canvasInUse = new Set<HTMLCanvasElement>();
let tasks: FakeTask[] = [];
let sameCanvasViolations: string[] = [];
/** Pages whose render() ran to completion. */
let completed: number[] = [];
/** Every render() call, in order, with the bitmap width it was asked to paint. */
let started: { page: number; width: number; overlay: boolean }[] = [];
/** Page numbers whose render should reject immediately, as a corrupt page would. */
let failingPages = new Set<number>();
/** Same, but only for the overlay pass — the template pass for that page still succeeds. */
let failingOverlayPages = new Set<number>();

const PAGE_PT = { width: 612, height: 792 };

function makePage(pageNumber: number) {
  return {
    getViewport: ({ scale }: { scale: number }) => ({
      width: PAGE_PT.width * scale,
      height: PAGE_PT.height * scale,
    }),
    render: ({
      canvas,
      viewport,
      background,
    }: {
      canvas: HTMLCanvasElement;
      viewport: { width: number };
      background?: string;
    }) => {
      // The overlay pass is the only caller that asks for a transparent background.
      const overlay = background !== undefined;
      started.push({ page: pageNumber, width: Math.ceil(viewport.width), overlay });
      if (canvasInUse.has(canvas)) {
        // Exactly what pdf.js does, and the failure this ticket exists to prevent.
        sameCanvasViolations.push(`page ${pageNumber} started on a canvas already rendering`);
      }
      canvasInUse.add(canvas);

      let settle!: { resolve: () => void; reject: (e: unknown) => void };
      const promise = new Promise<void>((resolve, reject) => {
        settle = { resolve, reject };
      });
      const task: FakeTask = {
        promise,
        canvas,
        pageNumber,
        settled: false,
        overlay,
        cancel: () => {
          if (task.settled) return;
          task.settled = true;
          canvasInUse.delete(canvas); // synchronous, as in InternalRenderTask.cancel()
          settle.reject(new Error('RenderingCancelledException'));
        },
        finish: () => {
          if (task.settled) return;
          task.settled = true;
          canvasInUse.delete(canvas);
          completed.push(pageNumber);
          settle.resolve();
        },
      };
      tasks.push(task);
      if (failingPages.has(pageNumber) || (overlay && failingOverlayPages.has(pageNumber))) {
        task.settled = true;
        canvasInUse.delete(canvas);
        settle.reject(new Error('bad page'));
      }
      return task;
    },
  };
}

const destroy = vi.fn();
vi.mock('../pdfjsLoader', () => ({
  loadPdfDocument: vi.fn(async () => ({
    numPages: 2,
    getPage: async (n: number) => makePage(n),
    destroy,
  })),
}));

const { default: PdfCanvas } = await import('../PdfCanvas');

/** Settle every task that is still pending, letting the paint loop advance. */
function finishPending() {
  for (const t of [...tasks]) if (!t.settled) t.finish();
}

let originalGetContext: typeof HTMLCanvasElement.prototype.getContext;
let originalIO: typeof IntersectionObserver | undefined;

beforeEach(() => {
  canvasInUse.clear();
  tasks = [];
  sameCanvasViolations = [];
  completed = [];
  started = [];
  failingPages = new Set();
  failingOverlayPages = new Set();

  // happy-dom returns null for a 2d context, which would make PdfCanvas skip every page.
  originalGetContext = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = (() => ({
    clearRect: () => {},
    drawImage: () => {},
    setTransform: () => {},
  })) as unknown as typeof originalGetContext;

  // happy-dom defines IntersectionObserver but never delivers callbacks, so pages would never be
  // marked visible and the zoom tier could not move. Report everything as on screen, like a real
  // browser showing a short document.
  originalIO = globalThis.IntersectionObserver;
  class ImmediateIO {
    constructor(private readonly cb: IntersectionObserverCallback) {}
    observe(el: Element) {
      this.cb(
        [{ target: el, isIntersecting: true } as unknown as IntersectionObserverEntry],
        this as unknown as IntersectionObserver,
      );
    }
    unobserve() {}
    disconnect() {}
  }
  globalThis.IntersectionObserver = ImmediateIO as unknown as typeof IntersectionObserver;

  Object.defineProperty(window, 'devicePixelRatio', { value: 1, configurable: true });
});

afterEach(() => {
  HTMLCanvasElement.prototype.getContext = originalGetContext;
  globalThis.IntersectionObserver = originalIO as typeof IntersectionObserver;
});

/**
 * ONE stable buffer for the whole file. A fresh ArrayBuffer per rerender would re-run Phase 1,
 * unmount the canvases and mount new ones — and a same-canvas collision cannot happen across two
 * different canvases, so every interleaving assertion below would pass for the wrong reason.
 */
const PDF_BYTES = new Uint8Array([1, 2, 3]).buffer;
const OVERLAY_BYTES = new Uint8Array([4, 5, 6]).buffer;

describe('PdfCanvas re-rasterization', () => {
  it('never starts a second render on a canvas whose previous render is still live', async () => {
    const r = await renderComponent(
      <PdfCanvas pdfBytes={PDF_BYTES} pageWidthPx={PAGE_PT.width} zoomScale={1} />,
    );

    // Two zoom changes land while the first page's render is still in flight — the interleaving
    // that a delete-then-await cancel path is accused of mishandling.
    await r.rerender(<PdfCanvas pdfBytes={PDF_BYTES} pageWidthPx={PAGE_PT.width} zoomScale={2} />);
    await r.rerender(<PdfCanvas pdfBytes={PDF_BYTES} pageWidthPx={PAGE_PT.width} zoomScale={3} />);

    // Without this, the empty-violation assertion would pass vacuously: a cancel path that
    // deadlocks never starts a second render, and never colliding is not the same as working.
    const page1Renders = started.filter((r) => r.page === 1);
    expect(page1Renders.length).toBeGreaterThanOrEqual(2);
    expect(sameCanvasViolations).toEqual([]);
    r.unmount();
  });

  it('paints the latest zoom level, not an intermediate one', async () => {
    const r = await renderComponent(
      <PdfCanvas pdfBytes={PDF_BYTES} pageWidthPx={PAGE_PT.width} zoomScale={1} />,
    );
    await r.rerender(<PdfCanvas pdfBytes={PDF_BYTES} pageWidthPx={PAGE_PT.width} zoomScale={2} />);
    await r.rerender(<PdfCanvas pdfBytes={PDF_BYTES} pageWidthPx={PAGE_PT.width} zoomScale={3} />);
    finishPending();
    await r.rerender(<PdfCanvas pdfBytes={PDF_BYTES} pageWidthPx={PAGE_PT.width} zoomScale={3} />);

    // tier 3 for cssScale 3 at dpr 1. Asserted on the render that was actually issued and
    // completed — canvas.width is assigned before render() and would hold even if none ran.
    const page1Renders = started.filter((r) => r.page === 1);
    expect(page1Renders[page1Renders.length - 1]?.width).toBe(PAGE_PT.width * 3);
    expect(completed).toContain(1);
    expect(r.get<HTMLCanvasElement>('canvas.pdf-canvas-bitmap').style.width).toBe(
      `${PAGE_PT.width}px`,
    );
    expect(sameCanvasViolations).toEqual([]);
    r.unmount();
  });

  it('keeps painting later pages when an earlier page fails', async () => {
    failingPages.add(1);
    const r = await renderComponent(
      <PdfCanvas pdfBytes={PDF_BYTES} pageWidthPx={PAGE_PT.width} zoomScale={1} />,
    );
    finishPending();
    await r.rerender(<PdfCanvas pdfBytes={PDF_BYTES} pageWidthPx={PAGE_PT.width} zoomScale={1} />);

    // The regression: page 1's rejection used to escape the loop and strand every page after it.
    expect(completed).toContain(2);
    r.unmount();
  });

  it('keeps painting later overlay pages when an earlier overlay page fails', async () => {
    failingOverlayPages.add(1);
    const r = await renderComponent(
      <PdfCanvas
        pdfBytes={PDF_BYTES}
        overlayPdfBytes={OVERLAY_BYTES}
        pageWidthPx={PAGE_PT.width}
        zoomScale={1}
      />,
    );
    finishPending();
    await r.rerender(
      <PdfCanvas
        pdfBytes={PDF_BYTES}
        overlayPdfBytes={OVERLAY_BYTES}
        pageWidthPx={PAGE_PT.width}
        zoomScale={1}
      />,
    );

    // Same rule as the template pass (LOOK-6375's overlay loop had the defect too): abandoning
    // the loop would leave page 2 showing the *previous* overlay's values.
    const overlayRenders = started.filter((x) => x.overlay);
    expect(overlayRenders.map((x) => x.page)).toContain(2);
    r.unmount();
  });
});
