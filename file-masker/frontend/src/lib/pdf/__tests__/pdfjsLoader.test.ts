import { describe, it, expect, vi, beforeEach } from 'vitest';
import { PDFDocument } from 'pdf-lib';

// pdf.js, by default, transfers the typed array's underlying ArrayBuffer to its
// worker thread — which DETACHES it on the caller's side (byteLength -> 0). The
// real pdf.js worker can't run under happy-dom, so we mock getDocument to
// faithfully reproduce that transfer behavior and assert that our code survives
// it. The assertions below are about real outcomes (caller's bytes still usable,
// pdf-lib can still parse them), not about how loadPdfDocument is implemented.
const { getDocumentMock } = vi.hoisted(() => ({ getDocumentMock: vi.fn() }));
vi.mock('pdfjs-dist', () => ({
  GlobalWorkerOptions: { workerSrc: '' },
  getDocument: getDocumentMock,
}));

import { loadPdfDocument } from '../pdfjsLoader';

function pdfJsTransferring(numPages = 1) {
  return ({ data }: { data: Uint8Array }) => {
    // Mimic pdf.js taking ownership of the buffer (transfer => detach).
    structuredClone(data.buffer, { transfer: [data.buffer] });
    return { promise: Promise.resolve({ numPages }) };
  };
}

describe('loadPdfDocument', () => {
  beforeEach(() => {
    getDocumentMock.mockReset();
    getDocumentMock.mockImplementation(pdfJsTransferring());
  });

  it('leaves the caller-provided Uint8Array intact after pdf.js consumes it', async () => {
    const original = new Uint8Array([0x25, 0x50, 0x44, 0x46]); // %PDF

    await loadPdfDocument(original);

    expect(original.byteLength).toBe(4);
    expect(Array.from(original)).toEqual([0x25, 0x50, 0x44, 0x46]);
  });

  it('leaves the caller-provided ArrayBuffer intact after pdf.js consumes it', async () => {
    const buffer = new Uint8Array([5, 6, 7, 8]).buffer;

    await loadPdfDocument(buffer);

    expect(buffer.byteLength).toBe(4);
  });
});

describe('multi-page source workflow (loadPdfDocument -> pdf-lib re-read)', () => {
  beforeEach(() => {
    getDocumentMock.mockReset();
  });

  // Reproduces preparePdfSource's real sequence on a genuine multi-page PDF:
  // pdf.js reads the source first (to render page 1 / count pages), then pdf-lib
  // re-reads the SAME source bytes to extract pages 2+. The original bug threw
  // "No PDF header found" at the pdf-lib step because pdf.js had detached the
  // shared buffer. This test is implementation-agnostic: it only cares that the
  // source bytes remain parseable by pdf-lib after pdf.js has consumed them.
  it('lets pdf-lib re-read the same source bytes after pdf.js reads them', async () => {
    const src = await PDFDocument.create();
    src.addPage([200, 200]);
    src.addPage([200, 200]);
    src.addPage([200, 200]);
    const sourceBytes = new Uint8Array(await src.save());

    getDocumentMock.mockImplementation(pdfJsTransferring(3));

    // Step 1: pdf.js consumes the source bytes.
    const pdfjsDoc = await loadPdfDocument(sourceBytes);
    expect(pdfjsDoc.numPages).toBe(3);

    // Step 2: pdf-lib must still parse the SAME bytes (the regression point).
    const reread = await PDFDocument.load(sourceBytes);
    expect(reread.getPageCount()).toBe(3);
  });
});
