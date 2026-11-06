import { loadPdfDocument } from './pdfjsLoader';

/**
 * Loads a PDF and returns just its page count. Used by the create-template
 * flow to derive `page_count` for the API call without rendering anything.
 */
export async function getPdfPageCount(pdfBytes: Uint8Array): Promise<number> {
  const pdfDocument = await loadPdfDocument(pdfBytes);
  try {
    return pdfDocument.numPages;
  } finally {
    pdfDocument.destroy();
  }
}
