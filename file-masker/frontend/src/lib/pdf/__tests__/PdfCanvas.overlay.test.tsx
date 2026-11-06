import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderComponent, flush } from '../../../testing/renderComponent';

/**
 * The overlay layer repaints on every debounced preview (LOOK-6375), so overlays replace each other
 * far more often than the template underneath ever reloads. pdf.js refuses concurrent `render()`
 * calls on one canvas, which makes cancelling the outgoing paint a correctness requirement rather
 * than an optimisation: without it the replacement paint is rejected and the *stale* one finishes.
 */

/** A RenderTask whose promise only settles when the test says so — models a paint still in flight. */
function pendingRenderTask() {
  let resolve!: () => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<void>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  const task = {
    promise,
    cancel: vi.fn(() => {
      // pdf.js rejects the promise of a cancelled task.
      reject(new Error('RenderingCancelledException'));
    }),
    finish: () => resolve(),
  };
  return task;
}

const renderTasks: ReturnType<typeof pendingRenderTask>[] = [];

function fakePage() {
  return {
    getViewport: ({ scale }: { scale: number }) => ({ width: 600 * scale, height: 800 * scale }),
    render: vi.fn(() => {
      const task = pendingRenderTask();
      renderTasks.push(task);
      return task;
    }),
  };
}

const loadPdfDocument = vi.fn(async () => ({
  numPages: 1,
  getPage: vi.fn(async () => fakePage()),
  destroy: vi.fn(),
}));

// PdfCanvas imports the loader directly (the barrel re-exports PdfCanvas itself, so importing it
// from there would be circular). Mocking the barrel here would silently stop intercepting.
vi.mock('../pdfjsLoader', () => ({
  loadPdfDocument: (...args: unknown[]) => loadPdfDocument(...(args as [])),
}));

// Imported after the mock so PdfCanvas picks up the stubbed loader.
const { default: PdfCanvas } = await import('../PdfCanvas');

const TEMPLATE = new ArrayBuffer(8);

function overlayBytes(seed: number): ArrayBuffer {
  const bytes = new Uint8Array(8);
  bytes[0] = seed;
  return bytes.buffer;
}

describe('PdfCanvas overlay layer', () => {
  beforeEach(() => {
    renderTasks.length = 0;
    loadPdfDocument.mockClear();
    // happy-dom's canvas has no 2d context, and the component skips any canvas without one — so
    // without this stub these tests would pass through the paint path entirely and prove nothing.
    HTMLCanvasElement.prototype.getContext = vi.fn(() => ({
      clearRect: vi.fn(),
    })) as unknown as HTMLCanvasElement['getContext'];
  });

  it('cancels the in-flight overlay paint before starting the replacement', async () => {
    const result = await renderComponent(
      <PdfCanvas pdfBytes={TEMPLATE} overlayPdfBytes={overlayBytes(1)} />,
    );
    await flush();

    const beforeReplacement = renderTasks.length;
    expect(beforeReplacement).toBeGreaterThan(0);
    const outgoing = renderTasks[beforeReplacement - 1];
    expect(outgoing.cancel).not.toHaveBeenCalled();

    // A second preview arrives while the first is still painting.
    await result.rerender(<PdfCanvas pdfBytes={TEMPLATE} overlayPdfBytes={overlayBytes(2)} />);
    await flush();

    expect(outgoing.cancel).toHaveBeenCalled();
  });

  it('cancels the in-flight overlay paint on unmount', async () => {
    const result = await renderComponent(
      <PdfCanvas pdfBytes={TEMPLATE} overlayPdfBytes={overlayBytes(1)} />,
    );
    await flush();

    const outgoing = renderTasks[renderTasks.length - 1];
    result.unmount();
    await flush();

    expect(outgoing.cancel).toHaveBeenCalled();
  });
});
