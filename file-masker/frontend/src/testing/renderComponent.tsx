import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach } from 'vitest';

/**
 * Minimal interactive render harness for component tests.
 *
 * <p>The project's existing component tests use {@code renderToStaticMarkup}, which is enough to
 * assert markup but cannot click anything. Characterization tests for a page whose behaviour *is*
 * interaction (switching tabs, enabling a button) need a real client render, so this wraps
 * {@code createRoot} + React 19's {@code act} against the already-configured happy-dom environment.
 *
 * <p>Deliberately not `@testing-library/react`: this is ~40 lines, adds no dependency, and keeps the
 * repo's test stack as it is. Add the library if these helpers ever start growing a query DSL.
 *
 * <h2>Everything it mounts is unmounted after each test</h2>
 *
 * <p>A component left mounted keeps its timers armed. When the file's tests finish, vitest tears the
 * DOM environment down, and any timer that fires afterwards runs against a `window` that no longer
 * exists — vitest reports it as an unhandled error and fails the run even though every test passed.
 *
 * <p>That is not hypothetical: `compliance-tools-frontend-test` #249 failed exactly this way
 * (`ReferenceError: window is not defined` from `usePreviewOverlay`'s 500 ms debounce), on an agent
 * roughly 9x slower than usual — the timing window only opens when teardown lands inside the
 * debounce. 27 of the 37 files using this helper never unmounted, so the leak was one slow agent
 * away in most of them, not one file's oversight. Cleaning up here fixes the class; this is the same
 * thing `@testing-library`'s auto-cleanup does.
 */
export interface RenderResult {
  container: HTMLElement;
  /** Re-render with a new element (same root), flushed. */
  rerender: (element: ReactElement) => Promise<void>;
  unmount: () => void;
  /** Every element matching the selector, as an array. */
  all: <T extends Element = HTMLElement>(selector: string) => T[];
  /** First element matching the selector, or null. */
  find: <T extends Element = HTMLElement>(selector: string) => T | null;
  /** First element matching the selector, or throw — for the "must exist" case. */
  get: <T extends Element = HTMLElement>(selector: string) => T;
  /** Visible text content of the container, whitespace-collapsed. */
  text: () => string;
}

/** `IS_REACT_ACT_ENVIRONMENT` must be on or React 19 warns on every act() call. */
function enableActEnvironment() {
  (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
}

/** Roots mounted by the current test, torn down after it. */
const mounted = new Set<{ root: Root; container: HTMLElement }>();

afterEach(() => {
  for (const entry of mounted) {
    act(() => entry.root.unmount());
    entry.container.remove();
  }
  mounted.clear();
});

export async function renderComponent(element: ReactElement): Promise<RenderResult> {
  enableActEnvironment();
  const container = document.createElement('div');
  document.body.appendChild(container);
  let root: Root;
  await act(async () => {
    root = createRoot(container);
    root.render(element);
  });
  const entry = { root: root!, container };
  mounted.add(entry);
  return {
    container,
    rerender: async (next: ReactElement) => {
      await act(async () => {
        root.render(next);
      });
    },
    unmount: () => {
      // 幂等: 已有 10 个测试文件显式调用它, 之后 afterEach 不该再 unmount 一次。
      if (!mounted.delete(entry)) return;
      act(() => root.unmount());
      container.remove();
    },
    all: <T extends Element = HTMLElement>(selector: string) =>
      [...container.querySelectorAll(selector)] as T[],
    find: <T extends Element = HTMLElement>(selector: string) =>
      container.querySelector(selector) as T | null,
    get: <T extends Element = HTMLElement>(selector: string) => {
      const found = container.querySelector(selector);
      if (!found) throw new Error(`no element matching ${selector}`);
      return found as T;
    },
    text: () => (container.textContent ?? '').replace(/\s+/g, ' ').trim(),
  };
}

/** Click an element and flush the resulting React work. */
export async function click(element: Element | null): Promise<void> {
  if (!element) throw new Error('click() called with no element');
  await act(async () => {
    element.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
  });
}

/**
 * Type into an input/textarea the way React's onChange expects.
 *
 * <p>The value MUST be written through the native prototype setter. React installs its own setter on
 * the element to track the last value it rendered; assigning `input.value = x` directly goes through
 * that setter, so React concludes the value never changed and skips onChange entirely — the field
 * would look typed-in while the component's state stayed empty. Bypassing to the prototype setter is
 * the same trick testing-library uses.
 */
export async function type(element: Element | null, value: string): Promise<void> {
  if (!element) throw new Error('type() called with no element');
  const input = element as HTMLInputElement | HTMLTextAreaElement;
  const proto =
    input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const nativeSetter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
  await act(async () => {
    if (nativeSetter) nativeSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

/**
 * Choose an option in a `<select>`.
 *
 * <p>Same native-setter trick as {@link type} — React tracks the last value it rendered on the
 * element itself, so a direct assignment looks like "nothing changed" and onChange never fires.
 * The event is `change`, not `input`: that is what React's onChange maps to for a select.
 */
export async function selectOption(element: Element | null, value: string): Promise<void> {
  if (!element) throw new Error('selectOption() called with no element');
  const select = element as HTMLSelectElement;
  const nativeSetter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set;
  await act(async () => {
    if (nativeSetter) nativeSetter.call(select, value);
    else select.value = value;
    select.dispatchEvent(new Event('change', { bubbles: true }));
  });
}

/** Let queued microtasks / promise chains settle, then flush React. */
export async function flush(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
  });
}

/** The element whose accessible label matches exactly — labels are the stable handle in this app. */
export function byLabel<T extends Element = HTMLElement>(
  result: RenderResult,
  label: string,
): T | null {
  return result.all<Element>('[aria-label]').find(
    (el) => el.getAttribute('aria-label') === label,
  ) as T | null;
}

/** Buttons whose trimmed text content matches exactly. */
export function byButtonText(result: RenderResult, text: string): HTMLButtonElement | null {
  return (
    result
      .all<HTMLButtonElement>('button')
      .find((b) => (b.textContent ?? '').replace(/\s+/g, ' ').trim() === text) ?? null
  );
}
