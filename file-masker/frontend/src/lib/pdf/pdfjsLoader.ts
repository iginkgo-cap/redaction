import { GlobalWorkerOptions, getDocument, type PDFDocumentProxy } from 'pdfjs-dist';
import pdfWorkerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';

GlobalWorkerOptions.workerSrc = pdfWorkerUrl;

export async function loadPdfDocument(
  bytes: Uint8Array | ArrayBuffer,
): Promise<PDFDocumentProxy> {
  // pdf.js transfers the typed array's underlying ArrayBuffer to its worker,
  // detaching it on the caller's side. Always hand it a private copy so callers
  // (e.g. pdfPipeline reading the same bytes with pdf-lib afterwards) keep theirs.
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  const data = view.slice();
  return getDocument({
    data,
    // 离线预览中文 PDF：cMap 与标准字体由 vite-plugin-static-copy 打包到本地（file-masker）
    cMapUrl: './cmaps/',
    cMapPacked: true,
    standardFontDataUrl: './standard_fonts/',
  }).promise;
}
