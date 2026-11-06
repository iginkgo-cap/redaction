import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { loadPdfDocument } from './pdfjsLoader';
import { pickRenderScale } from './renderScale';

type PdfDocument = Awaited<ReturnType<typeof loadPdfDocument>>;
type PdfPage = Awaited<ReturnType<PdfDocument['getPage']>>;
type RenderTask = ReturnType<PdfPage['render']>;

/**
 * How far outside the viewport a page still counts as visible. Pre-renders the page you are
 * scrolling toward, so it is already sharp by the time it arrives.
 */
const VISIBILITY_MARGIN = '200px';

export interface PdfPageMeta {
  /** 1-based page number, matching backend `Box.page`. */
  pageNumber: number;
  /** Rendered width in CSS pixels. */
  widthPx: number;
  /** Rendered height in CSS pixels. */
  heightPx: number;
  /** Underlying PDF page width in PDF points (1pt = 1/72 inch). Needed for
   *  overlays that want to convert PDF-points-based sizes (e.g. a field's
   *  fontSize from renderOpts) into preview pixels via `widthPx / widthPt`. */
  widthPt: number;
  /** Underlying PDF page height in PDF points. */
  heightPt: number;
}

export interface PagePos {
  /** Fraction of page width / height, in [0, 1] (origin top-left). */
  x: number;
  y: number;
}

interface Props {
  /** Accepts either form so callers can hand over whatever their fetch produced without an
   *  extra copy; this component copies once before pdf.js sees it either way. */
  pdfBytes: ArrayBuffer | Uint8Array;
  /** CSS width every page is rendered at. Default 900. */
  pageWidthPx?: number;
  /**
   * The CSS `transform: scale()` the pages are displayed at, from `ZoomApi.settledScale`
   * (LOOK-6391). Drives raster resolution only — never layout. Default 1.
   *
   * Pass the *settled* scale, not the live one: this triggers a repaint of every visible page.
   */
  zoomScale?: number;
  /**
   * Optional render prop for overlays drawn on top of each page (boxes,
   * selection rectangles, draft frames). Coordinate system is page-local
   * CSS pixels: `(0, 0)` is top-left of the page.
   */
  renderOverlay?: (page: PdfPageMeta) => ReactNode;
  /** Notifies the parent once every page has finished rendering. */
  onPagesReady?: (pages: PdfPageMeta[]) => void;
  /**
   * Pointer-down on a page's empty (canvas) surface, not on any overlay
   * child. Sets pointer capture on the page so the matching up/move events
   * fire even if the pointer drifts off-page.
   */
  onPagePointerDown?: (page: PdfPageMeta, pos: PagePos, e: React.PointerEvent) => void;
  onPagePointerMove?: (page: PdfPageMeta, pos: PagePos, e: React.PointerEvent) => void;
  onPagePointerUp?: (page: PdfPageMeta, pos: PagePos, e: React.PointerEvent) => void;
  /** Cursor style on canvas (e.g. 'crosshair' while a tool is active). */
  canvasCursor?: 'default' | 'crosshair' | 'not-allowed';
  /**
   * Server-rendered content layer to paint over the template (LOOK-6375): a transparent PDF with
   * one page per template page, carrying the filled values and nothing else. Null while none has
   * arrived yet. Keeping it separate from `pdfBytes` is the point — the template is fetched and
   * painted once, while this is re-rendered on every edit.
   */
  overlayPdfBytes?: ArrayBuffer | null;
}

function posFor(e: React.PointerEvent, container: HTMLElement): PagePos {
  const rect = container.getBoundingClientRect();
  const x = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  const y = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));
  return { x, y };
}

function currentDevicePixelRatio(): number {
  return typeof window === 'undefined' ? 1 : window.devicePixelRatio;
}

export default function PdfCanvas({
  pdfBytes,
  pageWidthPx = 900,
  zoomScale = 1,
  renderOverlay,
  onPagesReady,
  onPagePointerDown,
  onPagePointerMove,
  onPagePointerUp,
  canvasCursor = 'default',
  overlayPdfBytes = null,
}: Props) {
  const [pages, setPages] = useState<PdfPageMeta[]>([]);
  const [error, setError] = useState<string | null>(null);
  // Pages currently on screen. Only these get the high-resolution treatment: a 30-page document
  // rastered at the top tier would hold hundreds of MB of bitmap, nearly all of it unseen.
  const [visiblePages, setVisiblePages] = useState<Set<number>>(new Set());
  const docRef = useRef<PdfDocument | null>(null);
  const canvasRefs = useRef<Map<number, HTMLCanvasElement>>(new Map());
  const overlayCanvasRefs = useRef<Map<number, HTMLCanvasElement>>(new Map());
  /** The overlay paint currently in flight, if any. A ref because the effect that cancels it is
   *  rarely the one that started it. */
  const overlayTaskRef = useRef<RenderTask | null>(null);
  const pageElRefs = useRef<Map<number, HTMLElement>>(new Map());
  /** Render scale each page's bitmap currently holds, so an unchanged tier costs nothing. */
  const paintedScales = useRef<Map<number, number>>(new Map());
  /** In-flight render tasks, keyed by page. Must be cancelled before re-rendering that canvas. */
  const renderTasks = useRef<Map<number, RenderTask>>(new Map());
  // Hold the latest onPagesReady in a ref so Phase 2 doesn't have to
  // depend on it (avoids spurious re-runs when the parent passes a fresh
  // inline callback each render).
  const onPagesReadyRef = useRef(onPagesReady);
  onPagesReadyRef.current = onPagesReady;
  /** The `pages` array we have already announced, so a re-raster doesn't re-announce it. */
  const announcedPages = useRef<PdfPageMeta[] | null>(null);

  /**
   * Stop whatever is painting this page and forget what it painted.
   *
   * pdf.js refuses to run two `render()` calls against one canvas, and a cancelled task keeps
   * painting until it notices — so the await matters. Without it, any zoom that lands mid-render
   * throws "Cannot use the same canvas during multiple render() operations".
   */
  const cancelRender = useCallback(async (pageNumber: number) => {
    const task = renderTasks.current.get(pageNumber);
    if (!task) return;
    renderTasks.current.delete(pageNumber);
    paintedScales.current.delete(pageNumber);
    task.cancel();
    await task.promise.catch(() => {});
  }, []);

  /**
   * Tear down every in-flight render synchronously, for when the document itself is going away.
   *
   * Unlike {@link cancelRender} this does not await: the canvases are about to unmount with the
   * page list, so nothing can collide on them, and awaiting here would let the teardown land
   * *after* the next document has loaded and destroy it instead.
   */
  const abortAllRenders = useCallback(() => {
    for (const task of renderTasks.current.values()) task.cancel();
    renderTasks.current.clear();
    paintedScales.current.clear();
  }, []);

  // Phase 1: load the PDF + compute per-page dimensions, then publish the
  // meta to state so React mounts the canvases at the right size.
  //
  // Deliberately independent of `zoomScale`: re-rasterizing is Phase 2's job, and re-running this
  // on every zoom would reload the whole document and blank the pages first.
  useEffect(() => {
    let cancelled = false;
    setError(null);
    setPages([]);
    setVisiblePages(new Set());
    announcedPages.current = null;
    abortAllRenders();
    if (docRef.current) {
      docRef.current.destroy();
      docRef.current = null;
    }

    void (async () => {
      try {
        const bytes = new Uint8Array(pdfBytes.slice(0));
        const doc = await loadPdfDocument(bytes);
        if (cancelled) {
          doc.destroy();
          return;
        }
        const meta: PdfPageMeta[] = [];
        for (let i = 1; i <= doc.numPages; i++) {
          if (cancelled) {
            doc.destroy();
            return;
          }
          const page = await doc.getPage(i);
          const base = page.getViewport({ scale: 1 });
          const scale = pageWidthPx / base.width;
          const viewport = page.getViewport({ scale });
          meta.push({
            pageNumber: i,
            widthPx: viewport.width,
            heightPx: viewport.height,
            widthPt: base.width,
            heightPt: base.height,
          });
        }
        if (cancelled) {
          doc.destroy();
          return;
        }
        docRef.current = doc;
        setPages(meta);
      } catch (err) {
        if (cancelled) return;
        const message = err instanceof Error ? err.message : String(err);
        setError(`Failed to render PDF: ${message}`);
      }
    })();

    return () => {
      cancelled = true;
      abortAllRenders();
      if (docRef.current) {
        docRef.current.destroy();
        docRef.current = null;
      }
    };
  }, [pdfBytes, pageWidthPx, abortAllRenders]);

  // Track which pages are on screen. IntersectionObserver measures post-transform boxes, so the
  // zoom engine's `scale()` is accounted for without any conversion on our side.
  useEffect(() => {
    if (pages.length === 0) return;
    if (typeof IntersectionObserver === 'undefined') {
      // No observer (jsdom/happy-dom, very old browsers): treat everything as visible. Sharpness
      // is the point; the memory ceiling is the optimization.
      setVisiblePages(new Set(pages.map((p) => p.pageNumber)));
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        setVisiblePages((prev) => {
          const next = new Set(prev);
          let changed = false;
          for (const entry of entries) {
            const pageNumber = Number((entry.target as HTMLElement).dataset.page);
            if (!Number.isFinite(pageNumber)) continue;
            if (entry.isIntersecting) {
              if (!next.has(pageNumber)) {
                next.add(pageNumber);
                changed = true;
              }
            } else if (next.delete(pageNumber)) {
              changed = true;
            }
          }
          return changed ? next : prev;
        });
      },
      { rootMargin: VISIBILITY_MARGIN },
    );
    for (const el of pageElRefs.current.values()) observer.observe(el);
    return () => observer.disconnect();
  }, [pages]);

  // Phase 2: paint each page onto its mounted canvas, at the resolution it will actually be shown
  // at. Runs after React commits the JSX produced by setPages(meta), so the canvas refs are
  // guaranteed to be populated. Re-runs when the zoom settles or the visible set changes — each
  // page is skipped unless its target resolution actually moved.
  useEffect(() => {
    if (pages.length === 0) return;
    const doc = docRef.current;
    if (!doc) return;
    let cancelled = false;
    const dpr = currentDevicePixelRatio();

    void (async () => {
      try {
        for (const meta of pages) {
          if (cancelled) return;
          const canvas = canvasRefs.current.get(meta.pageNumber);
          if (!canvas) continue;

          const target = pickRenderScale({
            cssScale: visiblePages.has(meta.pageNumber) ? zoomScale : 1,
            devicePixelRatio: dpr,
            pageWidthPx: meta.widthPx,
            pageHeightPx: meta.heightPx,
          });
          if (paintedScales.current.get(meta.pageNumber) === target) continue;

          await cancelRender(meta.pageNumber);
          if (cancelled) return;

          const context = canvas.getContext('2d');
          if (!context) continue;

          let task: RenderTask | null = null;
          try {
            const page = await doc.getPage(meta.pageNumber);
            if (cancelled) return;

            // widthPx/widthPt is the scale that produced the CSS layout size; times `target` gives
            // the bitmap. Derived from the meta rather than a fresh scale-1 viewport so a rotated
            // page cannot drift from the dimensions the overlays were laid out against.
            const viewport = page.getViewport({ scale: (meta.widthPx / meta.widthPt) * target });
            const bitmapWidth = Math.ceil(viewport.width);
            const bitmapHeight = Math.ceil(viewport.height);
            // Assigning width/height clears the canvas — fine, we repaint immediately. Guarded so
            // an unchanged size doesn't blank a page for no reason.
            if (canvas.width !== bitmapWidth) canvas.width = bitmapWidth;
            if (canvas.height !== bitmapHeight) canvas.height = bitmapHeight;

            task = page.render({ canvas, canvasContext: context, viewport });
            renderTasks.current.set(meta.pageNumber, task);
            await task.promise;
            paintedScales.current.set(meta.pageNumber, target);
          } catch {
            // Per page, never per document. pdf.js rejects a superseded render with
            // RenderingCancelledException, and letting that escape the loop would abandon every
            // page *after* this one — a zoom landing while page 3 of 10 paints would strand pages
            // 4-10 blank until something unrelated triggered another run. `paintedScales` keeps no
            // entry for this page, so the next run repaints it.
            if (cancelled) return;
          } finally {
            if (task && renderTasks.current.get(meta.pageNumber) === task) {
              renderTasks.current.delete(meta.pageNumber);
            }
          }
        }
        if (cancelled) return;
        if (announcedPages.current !== pages) {
          announcedPages.current = pages;
          onPagesReadyRef.current?.(pages);
        }
      } catch {
        // The doc may have been destroyed mid-flight when the input changed, or a render task
        // cancelled by a zoom that landed while we were painting; the next run picks it up.
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [pages, pageWidthPx, zoomScale, visiblePages, cancelRender]);

  // Phase 3: paint the server-rendered content layer over each template page. Runs independently
  // of Phases 1-2 so a new overlay never re-decodes the template — that separation is what makes a
  // per-keystroke preview affordable.
  useEffect(() => {
    if (pages.length === 0) return;
    let cancelled = false;

    if (overlayPdfBytes === null) {
      for (const meta of pages) {
        const canvas = overlayCanvasRefs.current.get(meta.pageNumber);
        canvas?.getContext('2d')?.clearRect(0, 0, canvas.width, canvas.height);
      }
      return;
    }

    (async () => {
      let doc: PdfDocument | null = null;
      try {
        // Stop the outgoing paint before touching the same canvas again. Previews replace each
        // other on a 500ms debounce, so overlaps are routine — and pdf.js refuses concurrent
        // render() on one canvas, which would reject the NEW paint and let the stale one finish.
        // Awaiting the cancellation matters as much as requesting it: cancel() only starts the
        // teardown, and the canvas is not free until the task's promise settles.
        const outgoing = overlayTaskRef.current;
        if (outgoing) {
          outgoing.cancel();
          await outgoing.promise.catch(() => {});
          overlayTaskRef.current = null;
        }
        if (cancelled) return;

        // pdf.js detaches the buffer it is handed, so the caller's copy must not be the one passed.
        const overlayDoc = await loadPdfDocument(new Uint8Array(overlayPdfBytes.slice(0)));
        doc = overlayDoc;
        if (cancelled) return;
        if (overlayDoc.numPages !== pages.length) {
          console.warn(
            `Overlay has ${overlayDoc.numPages} pages but the template has ${pages.length}; ` +
              'painting the pages they have in common.',
          );
        }
        for (const meta of pages) {
          if (cancelled) return;
          if (meta.pageNumber > overlayDoc.numPages) continue;
          const canvas = overlayCanvasRefs.current.get(meta.pageNumber);
          if (!canvas) continue;
          const context = canvas.getContext('2d');
          if (!context) continue;

          let task: RenderTask | null = null;
          try {
            const page = await overlayDoc.getPage(meta.pageNumber);
            if (cancelled) return;
            const base = page.getViewport({ scale: 1 });
            // Match the template bitmap exactly (LOOK-6391). Anything less and the filled values
            // are the one blurry thing on an otherwise sharp page — more conspicuous than the
            // uniform softness this ticket set out to remove.
            const overlayScale = pickRenderScale({
              cssScale: visiblePages.has(meta.pageNumber) ? zoomScale : 1,
              devicePixelRatio: currentDevicePixelRatio(),
              pageWidthPx: meta.widthPx,
              pageHeightPx: meta.heightPx,
            });
            const viewport = page.getViewport({
              scale: (pageWidthPx / base.width) * overlayScale,
            });
            const bitmapWidth = Math.ceil(viewport.width);
            const bitmapHeight = Math.ceil(viewport.height);
            if (canvas.width !== bitmapWidth) canvas.width = bitmapWidth;
            if (canvas.height !== bitmapHeight) canvas.height = bitmapHeight;
            context.clearRect(0, 0, canvas.width, canvas.height);
            // Transparent background: the template underneath must show through.
            task = page.render({
              canvas,
              canvasContext: context,
              viewport,
              background: 'rgba(0,0,0,0)',
            });
            overlayTaskRef.current = task;
            await task.promise;
          } catch {
            // Per page, never per document — same rule as the template pass. This page was
            // already cleared, so it is blank either way; abandoning the loop would additionally
            // leave every page *after* it showing the previous overlay's values, and stale values
            // on a filled form are a WYSIWYG lie rather than merely a missing paint.
            if (cancelled) return;
          } finally {
            if (overlayTaskRef.current === task) overlayTaskRef.current = null;
          }
        }
      } catch {
        // A superseded overlay (the next render is already on its way) or a malformed one — either
        // way the previous paint stays on screen rather than the page going blank.
      } finally {
        doc?.destroy();
      }
    })();

    return () => {
      cancelled = true;
      // Requested here, awaited by whichever effect paints next (or by nothing, on unmount).
      overlayTaskRef.current?.cancel();
    };
  }, [overlayPdfBytes, pages, pageWidthPx, zoomScale, visiblePages]);

  if (error) {
    return <div className="pdf-canvas-error">{error}</div>;
  }

  return (
    <div className="pdf-canvas-scroll">
      {pages.map((p) => (
        <div
          key={p.pageNumber}
          ref={(el) => {
            if (el) pageElRefs.current.set(p.pageNumber, el);
            else pageElRefs.current.delete(p.pageNumber);
          }}
          className="pdf-canvas-page"
          data-page={p.pageNumber}
          style={{ width: p.widthPx, height: p.heightPx, cursor: canvasCursor }}
          onPointerDown={(e) => {
            if (!onPagePointerDown) return;
            // Only fire when the user pressed on empty canvas, not on an
            // overlay child (those have their own click semantics).
            if (!(e.target instanceof HTMLCanvasElement)) return;
            e.currentTarget.setPointerCapture(e.pointerId);
            onPagePointerDown(p, posFor(e, e.currentTarget), e);
          }}
          onPointerMove={(e) => {
            if (!onPagePointerMove) return;
            if (!e.currentTarget.hasPointerCapture(e.pointerId)) return;
            onPagePointerMove(p, posFor(e, e.currentTarget), e);
          }}
          onPointerUp={(e) => {
            if (!onPagePointerUp) return;
            if (!e.currentTarget.hasPointerCapture(e.pointerId)) return;
            e.currentTarget.releasePointerCapture(e.pointerId);
            onPagePointerUp(p, posFor(e, e.currentTarget), e);
          }}
        >
          {/* CSS size is the layout contract (overlays, pointer fractions, the zoom sizer all
              read it); the bitmap size is set imperatively by the paint effect and is free to be
              a multiple of it. */}
          <canvas
            ref={(el) => {
              if (el) canvasRefs.current.set(p.pageNumber, el);
              else canvasRefs.current.delete(p.pageNumber);
            }}
            style={{ width: p.widthPx, height: p.heightPx }}
            className="pdf-canvas-bitmap"
          />
          <canvas
            ref={(el) => {
              if (el) overlayCanvasRefs.current.set(p.pageNumber, el);
              else overlayCanvasRefs.current.delete(p.pageNumber);
            }}
            style={{ width: p.widthPx, height: p.heightPx }}
            className="pdf-canvas-content-layer"
          />
          {renderOverlay && (
            <div className="pdf-canvas-overlay">{renderOverlay(p)}</div>
          )}
          <div className="pdf-canvas-page-label">Page {p.pageNumber}</div>
        </div>
      ))}
    </div>
  );
}
