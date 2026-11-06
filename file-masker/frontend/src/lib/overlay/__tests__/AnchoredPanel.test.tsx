import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Profiler, act, useRef, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';

import AnchoredPanel, { type AnchoredPanelProps } from '../AnchoredPanel';

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  window.innerHeight = 800;
  window.innerWidth = 1000;
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

/** Rect the anchor will report. happy-dom gives every element a zero rect otherwise. */
function rect(over: Partial<DOMRect> = {}): DOMRect {
  const base = { x: 100, y: 200, left: 100, top: 200, right: 300, bottom: 230, width: 200, height: 30 };
  return { ...base, ...over, toJSON: () => ({}) } as DOMRect;
}

type HarnessProps = Omit<AnchoredPanelProps, 'anchorRef' | 'children'> & {
  anchorRect?: DOMRect;
  /** Wraps the anchor in an overflow:hidden box — the thing that used to crop these panels. */
  clip?: boolean;
  /** Extra nesting between the clipping box and the anchor. */
  depth?: number;
  /** Callers render the panel conditionally; `false` keeps the trigger and drops the panel. */
  open?: boolean;
  /**
   * Anchor the panel to the wrapper `div` around the button instead of the button itself — which is
   * what half the real call sites do (`.template-trigger-row`, `.split-btn-group`, …). A plain div
   * is not focusable, so `anchor.focus()` on it is a no-op.
   */
  anchorWrapper?: boolean;
  children?: ReactNode;
};

/** Renders an anchor (optionally inside a clipping container) plus the panel under test. */
function Harness({ anchorRect, clip, depth = 0, open = true, anchorWrapper, children, ...panelProps }: HarnessProps) {
  const anchorRef = useRef<HTMLElement>(null);
  // Callback ref, not a render-body assignment: React attaches refs before running layout effects,
  // so the stub is in place by the time the panel measures. Assigning during render would miss the
  // first commit entirely (the ref is still null while the body runs).
  const attachAnchor = (el: HTMLElement | null) => {
    anchorRef.current = el;
    if (el) el.getBoundingClientRect = () => anchorRect ?? rect();
  };

  const triggerButton = (
    <button type="button" ref={anchorWrapper ? undefined : attachAnchor} data-testid="trigger">
      trigger
    </button>
  );
  let anchor: ReactNode = anchorWrapper ? (
    <div ref={attachAnchor} data-testid="anchor">
      {triggerButton}
    </div>
  ) : (
    <button type="button" ref={attachAnchor} data-testid="anchor">
      trigger
    </button>
  );
  for (let i = 0; i < depth; i++) anchor = <div className={`nest-${i}`}>{anchor}</div>;

  return (
    <div className={clip ? 'clipper' : undefined} data-testid="clipper" style={clip ? { overflow: 'hidden' } : undefined}>
      {anchor}
      {open && (
        <AnchoredPanel anchorRef={anchorRef} {...panelProps}>
          {children ?? (
            <button type="button" data-testid="item">
              item
            </button>
          )}
        </AnchoredPanel>
      )}
    </div>
  );
}

/** Re-measurement is coalesced into an animation frame, so give the scheduled frame a chance. */
async function frame() {
  await act(async () => {
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    await Promise.resolve();
  });
}

async function mount(props: HarnessProps = {}) {
  await act(async () => {
    root.render(<Harness {...props} />);
    await Promise.resolve();
  });
  // The rect stub only exists after the ref attaches, so one more measured pass.
  await act(async () => {
    window.dispatchEvent(new Event('resize'));
    await Promise.resolve();
  });
  await frame();
}

function panel(): HTMLElement {
  return document.querySelector('[data-anchored-panel]') as HTMLElement;
}

/**
 * Clamping the panel's far edge needs the panel's own width, and happy-dom reports a zero rect for
 * it. Stub the width, then re-measure.
 */
async function withPanelWidth(width: number) {
  panel().getBoundingClientRect = () => rect({ width }) as DOMRect;
  await act(async () => {
    window.dispatchEvent(new Event('resize'));
    await Promise.resolve();
  });
  await frame();
}

describe('AnchoredPanel — escapes clipping ancestors', () => {
  it('renders outside the anchor’s subtree so an overflow:hidden ancestor cannot crop it', async () => {
    await mount({ clip: true });

    const clipper = container.querySelector('[data-testid="clipper"]')!;
    expect(panel()).not.toBeNull();
    expect(clipper.contains(panel())).toBe(false);
    expect(panel().parentElement).toBe(document.body);
  });

  it('keeps the panel out of the clipping container even when the anchor is deeply nested', async () => {
    await mount({ clip: true, depth: 3 });

    expect(container.querySelector('[data-testid="clipper"]')!.contains(panel())).toBe(false);
  });

  it('renders into a caller-supplied container (a modal dialog keeps its panel in the top layer)', async () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    try {
      await mount({ container: host });
      expect(host.contains(panel())).toBe(true);
    } finally {
      host.remove();
    }
  });

  it('moves into a container that only arrives on a later render (LOOK-6005)', async () => {
    // Why callers must hold that element in state: a container read from `ref.current` during
    // render is null on the commit that mounts it, and a null container quietly falls back to
    // document.body — which, for a modal, means underneath the backdrop.
    const host = document.createElement('div');
    document.body.appendChild(host);
    try {
      await mount({ container: null });
      expect(panel().parentElement).toBe(document.body);

      await act(async () => {
        root.render(<Harness container={host} />);
        await Promise.resolve();
      });

      expect(host.contains(panel())).toBe(true);
    } finally {
      host.remove();
    }
  });
});

describe('AnchoredPanel — placement', () => {
  it('positions below the anchor when there is room', async () => {
    await mount({ anchorRect: rect({ top: 100, bottom: 130 }) });

    expect(panel().style.position).toBe('fixed');
    expect(panel().style.top).toBe('134px'); // bottom + 4px gap
    expect(panel().style.bottom).toBe('');
    // Surfaced so CSS can react to the flip (shadow direction) without the caller tracking it.
    expect(panel().dataset.placement).toBe('bottom');
  });

  it('flips above the anchor when the space below is too small', async () => {
    // Anchor near the viewport floor: 800 - 770 = 30px below, but 740px above.
    await mount({ anchorRect: rect({ top: 740, bottom: 770 }) });

    expect(panel().style.bottom).toBe('64px'); // innerHeight - top + gap
    expect(panel().style.top).toBe('');
    expect(panel().dataset.placement).toBe('top');
  });

  it('right-aligns to the anchor when asked, so a wide menu does not overflow the viewport', async () => {
    await mount({ align: 'right', anchorRect: rect({ left: 700, right: 900 }) });

    // right edge pinned to the anchor's right edge, measured from the viewport's right
    expect(panel().style.right).toBe('100px');
    expect(panel().style.left).toBe('');
  });

  it('keeps its viewport margin when the anchor sits flush against the edge', async () => {
    await mount({ align: 'right', anchorRect: rect({ left: 800, right: 1000 }) });

    // Not 0px: hugging the edge looks broken, so the margin wins over exact alignment.
    expect(panel().style.right).toBe('8px');
  });

  it('matches the anchor width when asked', async () => {
    await mount({ matchAnchorWidth: true, anchorRect: rect({ width: 240 }) });

    expect(panel().style.width).toBe('240px');
  });

  it('never promises more height than the space it measured (LOOK-6005)', async () => {
    // A short window: 78px above the anchor, 68px below, and neither reaches the 140px minHeight.
    // `minHeight` used to act as a floor on the result, so the panel claimed 140px and poked out of
    // the viewport instead of shrinking.
    window.innerHeight = 200;
    await mount({ anchorRect: rect({ top: 90, bottom: 120 }) });

    expect(panel().dataset.placement).toBe('top'); // the roomier side still wins
    expect(panel().style.maxHeight).toBe('78px');
  });

  // `align` picks which edge lines up with the anchor; it is not licence for the other edge to leave
  // the screen. LOOK-6005 kept that edge on screen by clamping, which detaches the panel from its
  // trigger and slides it along the viewport edge. LOOK-6023 flips to the other side instead when
  // that side has room — the panel stays attached to the anchor, which is the point of anchoring it.
  // Clamping remains the fallback for when neither side fits; see below.
  it('flips a left-aligned panel to the right rather than let its right edge leave the viewport', async () => {
    await mount({ anchorRect: rect({ left: 900, right: 980 }) });
    await withPanelWidth(200);

    expect(panel().style.right).toBe('20px'); // 1000 - the anchor's 980, still touching the trigger
    expect(panel().style.left).toBe('');
  });

  it('flips a right-aligned panel to the left rather than let its left edge leave the viewport', async () => {
    await mount({ align: 'right', anchorRect: rect({ left: 120, right: 200 }) });
    await withPanelWidth(400);

    expect(panel().style.left).toBe('120px'); // the anchor's left, not pinned to the margin
    expect(panel().style.right).toBe('');
  });

  it('flips the workspace filler’s picker off a “+” sitting at the far left (LOOK-6023)', async () => {
    // The regression this was written for: a 22rem picker asked to right-align to a tab-strip "+"
    // ~40px from the window edge. Clamped, it ran off the left of the screen.
    await mount({ align: 'right', anchorRect: rect({ left: 40, right: 90 }) });
    await withPanelWidth(352);

    expect(panel().style.left).toBe('40px');
  });

  it('keeps the requested side when that side has room', async () => {
    await mount({ align: 'right', anchorRect: rect({ left: 600, right: 800 }) });
    await withPanelWidth(200);

    expect(panel().style.right).toBe('200px'); // 1000 - the anchor's 800
    expect(panel().style.left).toBe('');
  });

  /**
   * The side choice is only as good as the width it was made from, and a panel whose contents arrive
   * asynchronously is measured while it is still a spinner. The add-form picker loads its template
   * list after mount and roughly doubles in width when the rows land; placed against the spinner's
   * width, it had already committed to a side that no longer fits (LOOK-6023).
   *
   * <p>happy-dom has a ResizeObserver but no layout to drive it, so the observer is stubbed and
   * fired by hand — what is under test is that the panel observes itself and re-measures, not the
   * browser's own delivery of the callback.
   */
  it('re-measures when its own content changes size, not only on scroll and window resize', async () => {
    let fire: (() => void) | null = null;
    let observed: Element | null = null;
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(cb: () => void) {
          fire = cb;
        }
        observe(el: Element) {
          observed = el;
        }
        disconnect() {}
        unobserve() {}
      },
    );

    await mount({ align: 'right', anchorRect: rect({ left: 300, right: 350 }) });
    await withPanelWidth(200);
    // 200 fits in the 342px to the left of the anchor's right edge, so it stays where it was asked.
    expect(panel().style.right).toBe('650px');

    expect(observed).toBe(panel());
    panel().getBoundingClientRect = () => rect({ width: 600 }) as DOMRect;
    await act(async () => {
      fire?.();
      await Promise.resolve();
    });
    await frame();

    // 600 no longer fits on the right, and the 692px to the left of it does.
    expect(panel().style.left).toBe('300px');
    expect(panel().style.right).toBe('');
  });

  it('clamps rather than flips when neither side has room', async () => {
    // Flipping only helps if the other side is actually better. When nothing fits, the LOOK-6005
    // clamp is still what keeps the far edge on screen.
    await mount({ anchorRect: rect({ left: 900, right: 980 }) });
    await withPanelWidth(990);

    expect(panel().style.left).toBe('8px');
    expect(panel().style.right).toBe('');
  });

  it('will not be wider than the viewport', async () => {
    await mount({});

    expect(panel().style.maxWidth).toBe('984px'); // 1000 - 8 margin either side
  });

  it('pins a panel wider than the viewport to the margin', async () => {
    // The first measurement reads the panel's natural width, before its own max-width has been
    // applied — so for a very wide panel it measures 2000px where the rendered panel can only be
    // 984px. It makes no difference: the far-edge limit is `max(margin, 1000 - 8 - width)`, which
    // bottoms out at the margin for anything at or past 984px. Clamping the measured width to
    // max-width first would compute the identical 8px.
    await mount({ anchorRect: rect({ left: 900, right: 980 }) });
    await withPanelWidth(2000);

    expect(panel().style.left).toBe('8px');
    expect(panel().style.maxWidth).toBe('984px');
  });

  it('re-measures when an ancestor scrolls', async () => {
    let current = rect({ top: 100, bottom: 130 });
    await mount({ anchorRect: current });
    expect(panel().style.top).toBe('134px');

    // The anchor moves up as its scroll container scrolls; a stale panel would stay behind.
    // Commit the moved anchor FIRST — the rect stub is installed by the callback ref during commit,
    // so firing the scroll in the same act would still measure the old position.
    current = rect({ top: 50, bottom: 80 });
    await act(async () => {
      root.render(<Harness anchorRect={current} />);
      await Promise.resolve();
    });
    await act(async () => {
      document.dispatchEvent(new Event('scroll', { bubbles: true }));
      await Promise.resolve();
    });
    await frame();

    expect(panel().style.top).toBe('84px');
  });

  it('coalesces a burst of scroll events into one measurement (LOOK-6005)', async () => {
    // Scrolling a long list fires this dozens of times a second; measuring on every one of them
    // means a layout read (and, before this, a re-render) per event.
    await mount({});
    const anchor = container.querySelector('[data-testid="anchor"]')!;
    let measured = 0;
    anchor.getBoundingClientRect = () => {
      measured++;
      return rect();
    };

    await act(async () => {
      for (let i = 0; i < 5; i++) document.dispatchEvent(new Event('scroll', { bubbles: true }));
      await Promise.resolve();
    });
    await frame();

    expect(measured).toBe(1);
  });

  it('does not re-render when a scroll leaves the placement unchanged (LOOK-6005)', async () => {
    // An anchor that is scrolled but ends up in the same place (a scroll in an unrelated container,
    // an over-scroll bounce) used to produce a fresh placement object and a wasted render.
    let commits = 0;
    await act(async () => {
      root.render(
        <Profiler id="anchored" onRender={() => { commits++; }}>
          <Harness anchorRect={rect({ top: 100, bottom: 130 })} />
        </Profiler>,
      );
      await Promise.resolve();
    });
    await frame();
    const settled = commits;

    await act(async () => {
      document.dispatchEvent(new Event('scroll', { bubbles: true }));
      await Promise.resolve();
    });
    await frame();

    expect(commits).toBe(settled);
  });
});

describe('AnchoredPanel — the anchor scrolling away (LOOK-6005)', () => {
  /** Commits a moved anchor, then scrolls. The rect stub is installed during commit, so the
   *  re-render has to land before the scroll event or the panel measures the old position. */
  async function scrollAnchorTo(anchorRect: DOMRect, onClose: () => void, extra: HarnessProps = {}) {
    await act(async () => {
      root.render(<Harness {...extra} anchorRect={anchorRect} onClose={onClose} />);
      await Promise.resolve();
    });
    await act(async () => {
      document.dispatchEvent(new Event('scroll', { bubbles: true }));
      await Promise.resolve();
    });
    await frame();
  }

  it('closes when a scroll takes the anchor out of the viewport', async () => {
    // The kebab menu used to close on any scroll, and for a reason: it hangs off a table row, so a
    // menu left open over an unrelated row deletes a template the user can no longer see. The
    // migration dropped that listener; this is the replacement, and it covers all seven panels.
    const onClose = vi.fn();
    await mount({ onClose, anchorRect: rect({ top: 100, bottom: 130 }) });
    expect(onClose).not.toHaveBeenCalled();

    await scrollAnchorTo(rect({ top: -80, bottom: -50 }), onClose);

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('stays open and re-measures when the scroll only moves the anchor', async () => {
    const onClose = vi.fn();
    await mount({ onClose, anchorRect: rect({ top: 300, bottom: 330 }) });

    await scrollAnchorTo(rect({ top: 100, bottom: 130 }), onClose);

    expect(onClose).not.toHaveBeenCalled();
    expect(panel().style.top).toBe('134px');
  });

  it('closes when the anchor scrolls under the edge of its own scroll container', async () => {
    // Found in the browser, not here: the QuickForm table scrolls inside `.pdf-forms-accordion`,
    // not the window. A row scrolls under that container's edge — invisible — while its rect is
    // still comfortably inside the viewport, so a window-only check left the menu open over
    // unrelated rows. Which is the entire bug this contract exists to prevent.
    const onClose = vi.fn();
    await mount({ onClose, clip: true, anchorRect: rect({ top: 300, bottom: 330 }) });

    // The container's own box now sits below the row: the row has scrolled under its top edge.
    const clipper = container.querySelector('[data-testid="clipper"]') as HTMLElement;
    clipper.getBoundingClientRect = () => rect({ top: 400, bottom: 700, left: 0, right: 800 }) as DOMRect;

    await scrollAnchorTo(rect({ top: 300, bottom: 330 }), onClose, { clip: true });

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('leaves a panel open whose anchor was already off screen when it opened', async () => {
    // Only a scroll closes it. Opening is the caller's decision, and a panel that shut itself on
    // the first frame would be impossible to debug.
    const onClose = vi.fn();
    await mount({ onClose, anchorRect: rect({ top: -80, bottom: -50 }) });

    expect(onClose).not.toHaveBeenCalled();
  });
});

describe('AnchoredPanel — keyboard and assistive-tech association (LOOK-6005)', () => {
  /**
   * Absolute positioning inside the trigger's box gave these panels DOM adjacency for free: the
   * panel came right after its trigger, so Tab walked into it and a screen reader could find it.
   * The portal took that away and put nothing back — seven panels regressed at once.
   */
  function anchorEl(): HTMLElement {
    return container.querySelector('[data-testid="anchor"]') as HTMLElement;
  }

  async function press(key: string, init: KeyboardEventInit = {}) {
    await act(async () => {
      document.activeElement?.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, ...init }));
      await Promise.resolve();
    });
  }

  it('points the trigger at the panel with aria-owns and aria-controls', async () => {
    await mount({});

    expect(panel().id).not.toBe('');
    expect(anchorEl().getAttribute('aria-owns')).toBe(panel().id);
    expect(anchorEl().getAttribute('aria-controls')).toBe(panel().id);
  });

  it('drops the association again when the panel closes', async () => {
    await mount({});
    await act(async () => {
      root.render(<Harness open={false} />);
      await Promise.resolve();
    });

    expect(anchorEl().getAttribute('aria-owns')).toBeNull();
    expect(anchorEl().getAttribute('aria-controls')).toBeNull();
  });

  it('restores a caller-set association instead of eating it', async () => {
    // No caller sets these today, but a primitive that overwrites a trigger's own attributes and
    // then deletes them on close is a trap for the one that does.
    await mount({ open: false });
    const anchor = anchorEl();
    anchor.setAttribute('aria-controls', 'caller-owned');

    await act(async () => {
      root.render(<Harness />);
      await Promise.resolve();
    });
    expect(anchor.getAttribute('aria-controls')).toBe(panel().id); // the panel wins while open

    await act(async () => {
      root.render(<Harness open={false} />);
      await Promise.resolve();
    });

    expect(anchor.getAttribute('aria-controls')).toBe('caller-owned');
    expect(anchor.getAttribute('aria-owns')).toBeNull();
  });

  it('moves focus into the panel when Tab leaves the trigger', async () => {
    await mount({});
    anchorEl().focus();

    await press('Tab');

    expect(document.activeElement).toBe(document.querySelector('[data-testid="item"]'));
  });

  it('hands focus back to the trigger on Shift+Tab out of the panel', async () => {
    await mount({});
    (document.querySelector('[data-testid="item"]') as HTMLElement).focus();

    await press('Tab', { shiftKey: true });

    expect(document.activeElement).toBe(anchorEl());
  });

  it('closes and returns focus to the trigger when Tab leaves the last item', async () => {
    const onClose = vi.fn();
    await mount({ onClose });
    (document.querySelector('[data-testid="item"]') as HTMLElement).focus();

    await press('Tab');

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(anchorEl());
  });

  describe('when the anchor is a wrapper div, as it is at half the call sites', () => {
    // `.template-trigger-row`, `.ctc-sign-export-group`, `.pdf-filler-excel-dropdown` and two
    // `.split-btn-group`s anchor to the wrapper, not the button. A plain div is not focusable, so
    // `anchor.focus()` silently did nothing: Tab into the panel worked, Tab out closed it and
    // dropped focus on <body> — the keyboard user landed back at the top of the document.
    function triggerButton(): HTMLElement {
      return container.querySelector('[data-testid="trigger"]') as HTMLElement;
    }

    it('hands focus to the trigger inside it on Shift+Tab out of the panel', async () => {
      await mount({ anchorWrapper: true });
      (document.querySelector('[data-testid="item"]') as HTMLElement).focus();

      await press('Tab', { shiftKey: true });

      expect(document.activeElement).toBe(triggerButton());
    });

    it('hands focus back to that trigger when Tab leaves the last item', async () => {
      const onClose = vi.fn();
      await mount({ anchorWrapper: true, onClose });
      (document.querySelector('[data-testid="item"]') as HTMLElement).focus();

      await press('Tab');

      expect(onClose).toHaveBeenCalledTimes(1);
      expect(document.activeElement).toBe(triggerButton());
    });

    it('hands focus back to that trigger when the panel closes under a focused item', async () => {
      await mount({ anchorWrapper: true });
      (document.querySelector('[data-testid="item"]') as HTMLElement).focus();

      await act(async () => {
        root.render(<Harness anchorWrapper open={false} />);
        await Promise.resolve();
      });

      expect(document.activeElement).toBe(triggerButton());
    });

    it('still steps into the panel on Tab from the trigger', async () => {
      await mount({ anchorWrapper: true });
      triggerButton().focus();

      await press('Tab');

      expect(document.activeElement).toBe(document.querySelector('[data-testid="item"]'));
    });
  });

  it('returns focus to the trigger when the panel closes under a focused item', async () => {
    // Escape with focus on a menu item would otherwise drop focus on <body>, stranding the keyboard.
    await mount({});
    (document.querySelector('[data-testid="item"]') as HTMLElement).focus();

    await act(async () => {
      root.render(<Harness open={false} />);
      await Promise.resolve();
    });

    expect(document.activeElement).toBe(anchorEl());
  });
});

describe('AnchoredPanel — dismissal', () => {
  it('closes on a mousedown outside both the anchor and the panel', async () => {
    const onClose = vi.fn();
    await mount({ onClose });

    await act(async () => {
      document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
      await Promise.resolve();
    });

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('does NOT close on a mousedown inside the panel — the portal is not a DOM descendant of the anchor', async () => {
    // The regression this primitive exists to prevent: a naive `anchorEl.contains(target)` check
    // treats a portaled menu item as "outside", closes on mousedown, and the click never lands.
    const onClose = vi.fn();
    await mount({ onClose });

    await act(async () => {
      document
        .querySelector('[data-testid="item"]')!
        .dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
      await Promise.resolve();
    });

    expect(onClose).not.toHaveBeenCalled();
  });

  it('does not close on a mousedown on the anchor itself (the trigger owns its own toggle)', async () => {
    const onClose = vi.fn();
    await mount({ onClose });

    await act(async () => {
      container
        .querySelector('[data-testid="anchor"]')!
        .dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
      await Promise.resolve();
    });

    expect(onClose).not.toHaveBeenCalled();
  });

  it('closes on Escape', async () => {
    const onClose = vi.fn();
    await mount({ onClose });

    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      await Promise.resolve();
    });

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('stops Escape from reaching an enclosing dialog', async () => {
    const onClose = vi.fn();
    const outer = vi.fn();
    document.addEventListener('keydown', outer);
    try {
      await mount({ onClose });

      await act(async () => {
        panel().dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        await Promise.resolve();
      });

      expect(onClose).toHaveBeenCalledTimes(1);
      // A dialog listening further up must not also see it and close the whole dialog.
      expect(outer).not.toHaveBeenCalled();
    } finally {
      document.removeEventListener('keydown', outer);
    }
  });

  it('lets the caller consume Escape and keep the panel open (LOOK-6005)', async () => {
    // The panel listens in the capture phase, so it beats every handler inside the panel — which
    // turned "Escape cancels this inline edit form" into "Escape throws the whole dropdown away".
    const onClose = vi.fn();
    const onEscape = vi.fn(() => true);
    await mount({ onClose, onEscape });

    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      await Promise.resolve();
    });

    expect(onEscape).toHaveBeenCalledTimes(1);
    expect(onClose).not.toHaveBeenCalled();
  });

  it('closes on Escape when the caller declines to consume it (LOOK-6005)', async () => {
    const onClose = vi.fn();
    const onEscape = vi.fn(() => false);
    await mount({ onClose, onEscape });

    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      await Promise.resolve();
    });

    expect(onEscape).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('keeps a consumed Escape from reaching an enclosing dialog too (LOOK-6005)', async () => {
    // Consumed by the caller is still consumed: an enclosing <dialog> must not also act on it.
    const outer = vi.fn();
    document.addEventListener('keydown', outer);
    try {
      await mount({ onClose: vi.fn(), onEscape: () => true });

      await act(async () => {
        panel().dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        await Promise.resolve();
      });

      expect(outer).not.toHaveBeenCalled();
    } finally {
      document.removeEventListener('keydown', outer);
    }
  });
});
