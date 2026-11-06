import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
} from 'react';
import {
  MIN_SCALE,
  MAX_SCALE,
  clampScale,
  computeFitWidthScale,
  computeAnchoredScroll,
  zoomStep,
} from './zoomMath';

export interface ZoomApi {
  scale: number;
  /**
   * `scale` after it stops changing for {@link SETTLE_MS} (LOOK-6391). Drives re-rasterization,
   * which `scale` must not: a trackpad pinch emits a scale per frame, and re-painting a PDF page
   * on every one of them would drop frames for a sharpness nobody sees mid-gesture.
   */
  settledScale: number;
  percent: number;
  isFitWidth: boolean;
  zoomIn: () => void;
  zoomOut: () => void;
  fitWidth: () => void;
  actualSize: () => void;
  /** Callback ref for the scroll container. */
  containerRef: (node: HTMLDivElement | null) => void;
  /** Callback ref for the transformed content. */
  contentRef: (node: HTMLDivElement | null) => void;
  sizerStyle: CSSProperties;
  contentStyle: CSSProperties;
}

interface Options {
  minScale?: number;
  maxScale?: number;
}

/** How long zoom must hold still before consumers re-rasterize at the new resolution. */
const SETTLE_MS = 180;

export function useZoomPan({ minScale = MIN_SCALE, maxScale = MAX_SCALE }: Options = {}): ZoomApi {
  // The container/content nodes live in STATE (set via callback refs), not
  // plain refs. The preview DOM is mounted conditionally (only once a PDF /
  // preview has loaded), so a one-shot `[]` effect reading `ref.current` would
  // run before the nodes exist and never re-bind the ResizeObserver. Storing
  // the nodes in state makes the measurement effect re-run the moment they
  // mount (or remount), which is what keeps `natural` — and therefore the
  // sizer + fit-width — correct.
  const [container, setContainer] = useState<HTMLDivElement | null>(null);
  const [content, setContent] = useState<HTMLDivElement | null>(null);
  const [scale, setScaleState] = useState(1);
  const [settledScale, setSettledScale] = useState(1);
  const [isFitWidth, setIsFitWidth] = useState(true);
  const [natural, setNatural] = useState({ width: 0, height: 0 });
  const [containerWidth, setContainerWidth] = useState(0);
  const anchorRef = useRef<{
    oldScale: number;
    cursorX: number;
    cursorY: number;
    scrollLeft: number;
    scrollTop: number;
  } | null>(null);

  // Measure the content's natural (unscaled) size + the container width, and
  // keep them fresh via a ResizeObserver. Re-runs whenever either node
  // (re)mounts. offsetWidth/Height are NOT affected by CSS transform, so they
  // give the scale-1 footprint that sizer dimensions + fit-width are based on.
  useLayoutEffect(() => {
    if (!container || !content) return;
    const measure = () => {
      const w = content.offsetWidth;
      const h = content.offsetHeight;
      setNatural((p) => (p.width === w && p.height === h ? p : { width: w, height: h }));
      setContainerWidth((p) => (p === container.clientWidth ? p : container.clientWidth));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(content);
    ro.observe(container);
    return () => ro.disconnect();
  }, [container, content]);

  // While tracking fit-width, recompute scale whenever the container or the
  // content natural width changes (panel resize, pages first rendering).
  useLayoutEffect(() => {
    if (!isFitWidth) return;
    if (!(natural.width > 0) || !(containerWidth > 0)) return;
    setScaleState(computeFitWidthScale(containerWidth, natural.width, minScale, maxScale));
  }, [isFitWidth, natural.width, containerWidth, minScale, maxScale]);

  // After a scale commit that carried an anchor, adjust scroll so the point
  // under the cursor stays put.
  useLayoutEffect(() => {
    const a = anchorRef.current;
    if (!a || !container) return;
    anchorRef.current = null;
    const next = computeAnchoredScroll({
      oldScale: a.oldScale,
      newScale: scale,
      cursorX: a.cursorX,
      cursorY: a.cursorY,
      scrollLeft: a.scrollLeft,
      scrollTop: a.scrollTop,
    });
    container.scrollLeft = next.scrollLeft;
    container.scrollTop = next.scrollTop;
  }, [scale, container]);

  const zoomAtPoint = useCallback(
    (computeNext: (old: number) => number, cursorX: number, cursorY: number) => {
      if (!container) return;
      setIsFitWidth(false);
      setScaleState((old) => {
        const next = clampScale(computeNext(old), minScale, maxScale);
        if (next === old) return old;
        anchorRef.current = {
          oldScale: old,
          cursorX,
          cursorY,
          scrollLeft: container.scrollLeft,
          scrollTop: container.scrollTop,
        };
        return next;
      });
    },
    [container, minScale, maxScale],
  );

  // Ctrl/Cmd + wheel (and trackpad pinch, which arrives as wheel + ctrlKey) →
  // anchored zoom. Native non-passive listener so preventDefault works.
  useEffect(() => {
    if (!container) return;
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      const rect = container.getBoundingClientRect();
      const factor = Math.exp(-e.deltaY * 0.002);
      zoomAtPoint((old) => old * factor, e.clientX - rect.left, e.clientY - rect.top);
    };
    container.addEventListener('wheel', onWheel, { passive: false });
    return () => container.removeEventListener('wheel', onWheel);
  }, [container, zoomAtPoint]);

  const zoomIn = useCallback(() => {
    if (!container) return;
    zoomAtPoint(
      (old) => zoomStep(old, 1, minScale, maxScale),
      container.clientWidth / 2,
      container.clientHeight / 2,
    );
  }, [container, zoomAtPoint, minScale, maxScale]);

  const zoomOut = useCallback(() => {
    if (!container) return;
    zoomAtPoint(
      (old) => zoomStep(old, -1, minScale, maxScale),
      container.clientWidth / 2,
      container.clientHeight / 2,
    );
  }, [container, zoomAtPoint, minScale, maxScale]);

  const actualSize = useCallback(() => {
    if (!container) return;
    zoomAtPoint(() => 1, container.clientWidth / 2, container.clientHeight / 2);
  }, [container, zoomAtPoint]);

  const fitWidth = useCallback(() => setIsFitWidth(true), []);

  // Let `scale` settle before publishing it to whoever re-rasterizes. Each change restarts the
  // timer, so a continuous pinch produces exactly one commit — at the end.
  useEffect(() => {
    if (scale === settledScale) return;
    const handle = setTimeout(() => setSettledScale(scale), SETTLE_MS);
    return () => clearTimeout(handle);
  }, [scale, settledScale]);

  return {
    scale,
    settledScale,
    percent: Math.round(scale * 100),
    isFitWidth,
    zoomIn,
    zoomOut,
    fitWidth,
    actualSize,
    containerRef: setContainer,
    contentRef: setContent,
    sizerStyle: { width: natural.width * scale, height: natural.height * scale },
    contentStyle: { transform: `scale(${scale})`, transformOrigin: 'top left' },
  };
}
