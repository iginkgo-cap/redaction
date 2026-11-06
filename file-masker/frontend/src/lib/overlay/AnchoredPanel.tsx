import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from 'react';
import { createPortal } from 'react-dom';

/** Gap between the anchor and the panel, and the slack kept from the viewport edge. */
const DEFAULT_GAP_PX = 4;
const DEFAULT_VIEWPORT_MARGIN_PX = 8;
const DEFAULT_MIN_HEIGHT_PX = 140;

export interface AnchoredPanelProps {
  /** The element the panel hangs off — usually the button that opened it. */
  anchorRef: RefObject<HTMLElement | null>;
  /** Called for an outside mousedown or Escape. The caller owns the open state. */
  onClose?: () => void;
  /**
   * First refusal on Escape. Return `true` when the key was consumed by something *inside* the
   * panel — an inline edit form, a nested confirm — and the panel should stay open; anything else
   * falls through to `onClose`.
   *
   * This seam exists because the Escape listener below runs in the capture phase, so it beats every
   * handler inside the panel: `TemplateSelector`'s "Escape cancels this edit form" stopped firing
   * the moment the dropdown moved onto this primitive, and Escape threw away a half-written
   * template instead (LOOK-6005).
   */
  onEscape?: () => boolean;
  /** Which edge lines up with the anchor's matching edge. Default `left`. */
  align?: 'left' | 'right';
  /** Force the panel to the anchor's width (comboboxes want this; menus usually don't). */
  matchAnchorWidth?: boolean;
  /**
   * How much room below the anchor counts as "enough" — with less than this, the panel looks for
   * more room above and flips. Not a floor on the rendered height: the panel never claims more
   * space than exists, however short the window is.
   */
  minHeight?: number;
  /** Ceiling for the computed max-height. Default: whatever the viewport allows. */
  maxHeight?: number;
  gap?: number;
  viewportMargin?: number;
  /**
   * Where to portal to. Defaults to `document.body`, which is what escapes clipping ancestors.
   * Pass the dialog element when the anchor lives in a modal `<dialog>`: a body portal would render
   * *underneath* the backdrop, since only the dialog's own subtree is in the top layer.
   */
  container?: HTMLElement | null;
  className?: string;
  role?: string;
  'aria-label'?: string;
  'aria-labelledby'?: string;
  children: ReactNode;
}

interface Placement {
  left?: number;
  right?: number;
  top?: number;
  bottom?: number;
  width?: number;
  maxWidth: number;
  maxHeight: number;
  /** Which side of the anchor the panel ended up on — surfaced as `data-placement`. */
  side: 'top' | 'bottom';
}

const FOCUSABLE =
  'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

/** Tab stops inside the panel, in document order. */
function focusables(panel: HTMLElement): HTMLElement[] {
  return [...panel.querySelectorAll<HTMLElement>(FOCUSABLE)];
}

/**
 * Put focus back on the thing that opened the panel.
 *
 * Half the call sites anchor to a wrapper `div` rather than the button inside it
 * (`.template-trigger-row`, `.ctc-sign-export-group`, `.pdf-filler-excel-dropdown`, two
 * `.split-btn-group`s), and a plain div is not focusable — `anchor.focus()` there is a silent no-op
 * that dropped focus on `<body>`, i.e. the keyboard user landed back at the top of the document.
 * Fall back to the first focusable element inside the anchor, which is that trigger.
 */
function focusAnchor(anchor: HTMLElement): void {
  const target = anchor.matches(FOCUSABLE) ? anchor : anchor.querySelector<HTMLElement>(FOCUSABLE);
  (target ?? anchor).focus();
}

/** Overflow values that hide what runs past the box, as opposed to letting it draw outside. */
const CLIPS = /^(auto|scroll|hidden|clip)$/;

function outside(r: DOMRect, box: { top: number; bottom: number; left: number; right: number }): boolean {
  return r.bottom <= box.top || r.top >= box.bottom || r.right <= box.left || r.left >= box.right;
}

/**
 * The ancestors that can hide the anchor by clipping it. Computed once when the panel opens:
 * `getComputedStyle` forces a style recalculation, and the visibility check below runs on every
 * scroll frame — walking the whole chain each time would put back a good part of the per-event cost
 * the animation-frame coalescing exists to remove. Which ancestors clip does not change while a
 * panel is open; where their boxes are does, and that is read fresh each time.
 */
function clippingAncestors(el: HTMLElement): HTMLElement[] {
  const out: HTMLElement[] = [];
  for (let p = el.parentElement; p !== null; p = p.parentElement) {
    // The shorthand is read as well as the axes: browsers expand `overflow` into both, but not
    // every DOM implementation does, and a container declaring only the shorthand still clips.
    const style = getComputedStyle(p);
    if ([style.overflow, style.overflowX, style.overflowY].some((v) => CLIPS.test(v))) out.push(p);
  }
  return out;
}

/**
 * Entirely hidden — not merely clipped at one edge, where the panel is still meaningful.
 *
 * The viewport is not the only thing that can hide the anchor, and usually is not the one that
 * does: a kebab trigger lives in a table row inside `.pdf-forms-accordion`, and that row scrolls
 * under the container's own edge long before it goes anywhere near the window's. Checking the
 * window alone left the menu open, floating over unrelated rows, which is the whole bug.
 */
function isOutOfView(el: HTMLElement, clippers: HTMLElement[]): boolean {
  const r = el.getBoundingClientRect();
  if (outside(r, { top: 0, bottom: window.innerHeight, left: 0, right: window.innerWidth })) return true;
  return clippers.some((p) => outside(r, p.getBoundingClientRect()));
}

/** Same box in the same place — a scroll that did not move the anchor must not cause a render. */
function samePlacement(a: Placement | null, b: Placement): boolean {
  return (
    a !== null &&
    a.left === b.left &&
    a.right === b.right &&
    a.top === b.top &&
    a.bottom === b.bottom &&
    a.width === b.width &&
    a.maxWidth === b.maxWidth &&
    a.maxHeight === b.maxHeight &&
    a.side === b.side
  );
}

/**
 * A popover that cannot be clipped by whatever it happens to be rendered inside.
 *
 * Every dropdown in this app used to be `position: absolute` inside its trigger's box, which works
 * right up until somebody puts the trigger inside a panel that clips — `.ctc-picker-panel` pairs
 * `border-radius` with `overflow: hidden` so rows don't bleed past the corners, and it cropped the
 * export menu dead (LOOK-5986). `position: fixed` alone is not enough either: an ancestor with
 * `transform` / `filter` / `contain` becomes the containing block and clips it again, and this
 * codebase transforms things on hover all over the place. So the panel is *portaled out* of the
 * anchor's subtree entirely and positioned from the anchor's measured rect.
 *
 * That portal has one consequence worth knowing: the panel is no longer a DOM descendant of the
 * trigger, so a hand-rolled `triggerEl.contains(e.target)` dismissal check would treat clicks on
 * the panel's own items as "outside", close on mousedown, and swallow the click. Dismissal
 * therefore lives here, aware of both elements — callers should not add their own.
 */
export default function AnchoredPanel({
  anchorRef,
  onClose,
  onEscape,
  align = 'left',
  matchAnchorWidth = false,
  minHeight = DEFAULT_MIN_HEIGHT_PX,
  maxHeight,
  gap = DEFAULT_GAP_PX,
  viewportMargin = DEFAULT_VIEWPORT_MARGIN_PX,
  container,
  className,
  role,
  children,
  ...aria
}: AnchoredPanelProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const [placement, setPlacement] = useState<Placement | null>(null);
  /** What `placement` already holds — the dedupe key for the scroll path below. */
  const appliedRef = useRef<Placement | null>(null);
  const panelId = useId();

  const measure = useCallback(() => {
    const anchor = anchorRef.current;
    if (!anchor) return;
    const rect = anchor.getBoundingClientRect();
    const below = window.innerHeight - rect.bottom - gap - viewportMargin;
    const above = rect.top - gap - viewportMargin;
    // Drop down unless there is meaningfully more room above — flipping on a near-tie makes the
    // panel jump around while its content changes (e.g. a filtered list).
    const dropDown = below >= minHeight || below >= above;
    // `minHeight` decides which side to hang off; it is NOT a floor on the result. Raising the
    // max-height to it when neither side has that much room made the panel poke out of a short
    // window instead of shrinking (LOOK-6005).
    const room = Math.max(0, dropDown ? below : above);
    // `align` chooses which edge lines up with the anchor — the opposite edge still has to stay on
    // screen. Clamping only the near edge let a wide left-aligned menu slide off to the right.
    const panelWidth = matchAnchorWidth ? rect.width : (panelRef.current?.getBoundingClientRect().width ?? 0);
    const maxWidth = Math.max(0, window.innerWidth - viewportMargin * 2);
    const farEdgeLimit = Math.max(viewportMargin, window.innerWidth - viewportMargin - panelWidth);
    // `align` is a preference, not a command — the horizontal twin of the top/bottom flip above.
    // Left-aligning spends the room to the right of the anchor's left edge; right-aligning spends
    // the room to the left of its right edge. Flip when the preferred side cannot fit the panel and
    // the other can: clamping instead (LOOK-6005) keeps the far edge on screen but drags the panel
    // away from its trigger, which for a trigger near a window edge means a panel nowhere near the
    // thing that opened it (LOOK-6023). When neither side fits, the clamp below is still the
    // backstop.
    const roomLeftAligned = window.innerWidth - viewportMargin - rect.left;
    const roomRightAligned = rect.right - viewportMargin;
    const preferredRoom = align === 'right' ? roomRightAligned : roomLeftAligned;
    const otherRoom = align === 'right' ? roomLeftAligned : roomRightAligned;
    const effectiveAlign =
      preferredRoom < panelWidth && otherRoom >= panelWidth
        ? align === 'right'
          ? 'left'
          : 'right'
        : align;
    const next: Placement = {
      ...(effectiveAlign === 'right'
        ? { right: Math.min(Math.max(viewportMargin, window.innerWidth - rect.right), farEdgeLimit) }
        : { left: Math.min(Math.max(viewportMargin, rect.left), farEdgeLimit) }),
      ...(dropDown ? { top: rect.bottom + gap } : { bottom: window.innerHeight - rect.top + gap }),
      ...(matchAnchorWidth ? { width: rect.width } : {}),
      maxWidth,
      maxHeight: maxHeight === undefined ? room : Math.min(maxHeight, room),
      side: dropDown ? 'bottom' : 'top',
    };
    // Bail out before dispatching, not inside the updater: React can still re-render a component
    // whose setState returned the identical value, and this runs on every scroll frame.
    if (samePlacement(appliedRef.current, next)) return;
    appliedRef.current = next;
    setPlacement(next);
  }, [align, anchorRef, gap, matchAnchorWidth, maxHeight, minHeight, viewportMargin]);

  // Layout effect so the first painted frame is already in the right place.
  useLayoutEffect(measure, [measure]);

  useEffect(() => {
    // Scrolling a long list fires dozens of these a second, and each one is a layout read. Coalesce
    // into one animation frame — the panel cannot be repainted more often than that anyway.
    let frame = 0;
    let scrolled = false;
    function schedule(fromScroll: boolean) {
      scrolled = scrolled || fromScroll;
      if (frame !== 0) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        const checkVisibility = scrolled;
        scrolled = false;
        // A panel anchored to something you can no longer see is worse than no panel: the kebab menu
        // hangs off a table row, and one left open over an unrelated row deletes a template the user
        // cannot see. The row's own "close on scroll" listener was dropped in the migration, so the
        // primitive owns the contract now — for all of the panels, not just that one (LOOK-6005).
        const anchor = anchorRef.current;
        if (checkVisibility && onClose && anchor && isOutOfView(anchor, clippersRef.current)) {
          onClose();
          return;
        }
        measure();
      });
    }
    const onScroll = () => schedule(true);
    const onResize = () => schedule(false);
    window.addEventListener('resize', onResize);
    // Capture phase: a scroll in ANY ancestor moves the anchor, not just the window.
    window.addEventListener('scroll', onScroll, true);

    // The panel's own size is an input to placement — which side it hangs off and how far its far
    // edge reaches — and for a panel that loads its contents it is not known at mount. Without this,
    // a picker measured as a spinner keeps the placement it chose while it was narrow (LOOK-6023).
    // No visibility check: the panel growing says nothing about whether the anchor is still in view.
    //
    // This cannot run away: measure() bails on an unchanged placement before it sets state, so the
    // resize → measure → resize chain settles within a frame or two.
    const observer =
      typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => schedule(false));
    if (observer && panelRef.current) observer.observe(panelRef.current);

    return () => {
      if (frame !== 0) cancelAnimationFrame(frame);
      observer?.disconnect();
      window.removeEventListener('resize', onResize);
      window.removeEventListener('scroll', onScroll, true);
    };
  }, [anchorRef, measure, onClose]);

  /**
   * The portal broke the one thing absolute positioning gave away for free: the panel used to sit
   * right after its trigger in the DOM, so Tab walked into it and assistive tech could find it from
   * the trigger. `aria-owns` puts the panel back under the trigger in the accessibility tree; the
   * Tab handling below covers sequential focus, which `aria-owns` does not move (LOOK-6005).
   */
  useLayoutEffect(() => {
    const anchor = anchorRef.current;
    if (!anchor) return;
    // Put back whatever the trigger already said rather than deleting it: no caller sets these
    // today, but a primitive that swallows a trigger's own attributes is a trap for the one that does.
    const previous = (['aria-owns', 'aria-controls'] as const).map((name) => [name, anchor.getAttribute(name)] as const);
    anchor.setAttribute('aria-owns', panelId);
    anchor.setAttribute('aria-controls', panelId);
    return () => {
      for (const [name, value] of previous) {
        if (value === null) anchor.removeAttribute(name);
        else anchor.setAttribute(name, value);
      }
    };
  }, [anchorRef, panelId]);

  /** Which ancestors clip is fixed for the life of the panel; see `clippingAncestors`. */
  const clippersRef = useRef<HTMLElement[]>([]);
  useLayoutEffect(() => {
    const anchor = anchorRef.current;
    clippersRef.current = anchor === null ? [] : clippingAncestors(anchor);
  }, [anchorRef]);

  /** Focus must not be stranded on <body> when the panel it was in disappears. */
  useLayoutEffect(() => {
    const panel = panelRef.current;
    return () => {
      const anchor = anchorRef.current;
      if (panel && anchor && panel.contains(document.activeElement)) focusAnchor(anchor);
    };
  }, [anchorRef]);

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key !== 'Tab') return;
      const panel = panelRef.current;
      const anchor = anchorRef.current;
      if (!panel || !anchor) return;
      const active = document.activeElement;
      const items = focusables(panel);
      const last = items[items.length - 1];

      // Tab off the trigger steps into the panel, the way DOM adjacency used to.
      if (anchor.contains(active)) {
        const into = e.shiftKey ? last : items[0];
        if (!into) return;
        e.preventDefault();
        into.focus();
        return;
      }
      if (!panel.contains(active)) return;

      // Backwards off the front edge lands on the trigger, since that is where the panel hangs.
      if (e.shiftKey && active === items[0]) {
        e.preventDefault();
        focusAnchor(anchor);
        return;
      }
      // Forwards off the back edge is done with the panel: close it, put focus on the trigger, and
      // let the browser's own Tab carry on from there to whatever follows it.
      if (!e.shiftKey && active === last) {
        focusAnchor(anchor);
        onClose?.();
      }
    }
    document.addEventListener('keydown', onKeyDown, true);
    return () => document.removeEventListener('keydown', onKeyDown, true);
  }, [anchorRef, onClose]);

  useEffect(() => {
    if (!onClose) return;
    function onDocMouseDown(e: MouseEvent) {
      const target = e.target as Node;
      if (panelRef.current?.contains(target)) return;
      // The trigger toggles itself; closing here too would fight it and reopen on the same click.
      if (anchorRef.current?.contains(target)) return;
      onClose?.();
    }
    document.addEventListener('mousedown', onDocMouseDown);
    return () => document.removeEventListener('mousedown', onDocMouseDown);
  }, [anchorRef, onClose]);

  useEffect(() => {
    if (!onClose && !onEscape) return;
    function onKeyDown(e: KeyboardEvent) {
      if (e.key !== 'Escape') return;
      e.preventDefault();
      // Consumed either way — by the caller or by closing. Don't let it reach an enclosing
      // <dialog>, which would close the whole thing.
      e.stopPropagation();
      if (onEscape?.()) return;
      onClose?.();
    }
    document.addEventListener('keydown', onKeyDown, true);
    return () => document.removeEventListener('keydown', onKeyDown, true);
  }, [onClose, onEscape]);

  const host = container ?? document.body;
  return createPortal(
    <div
      ref={panelRef}
      id={panelId}
      data-anchored-panel=""
      data-placement={placement?.side}
      className={className}
      role={role}
      {...aria}
      style={{
        position: 'fixed',
        // Not measured yet (first paint): keep it out of sight rather than flashing at 0,0.
        ...(placement === null
          ? { visibility: 'hidden' }
          : {
              ...(placement.left !== undefined ? { left: placement.left } : {}),
              ...(placement.right !== undefined ? { right: placement.right } : {}),
              ...(placement.top !== undefined ? { top: placement.top } : {}),
              ...(placement.bottom !== undefined ? { bottom: placement.bottom } : {}),
              ...(placement.width !== undefined ? { width: placement.width } : {}),
              maxWidth: placement.maxWidth,
              maxHeight: placement.maxHeight,
            }),
      }}
    >
      {children}
    </div>,
    host,
  );
}
