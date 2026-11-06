import type { MaskKeyword } from './maskingSession';

/** 服务端返回的英文错误原因，可以直接展示给用户。 */
export class MaskRequestError extends Error {}

/** POST /mask：上传单个 PDF 和关键词，返回打码后的 PDF 字节（file-masker 本地服务）。 */
export async function maskPdf(file: File, keywords: MaskKeyword[]): Promise<Uint8Array> {
  const body = new FormData();
  body.append('file', file);
  body.append('keywords', JSON.stringify(keywords));
  const response = await fetch('mask', { method: 'POST', body });
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { error?: string } | null;
    throw new MaskRequestError(payload?.error ?? `Masking failed (HTTP ${response.status}).`);
  }
  return new Uint8Array(await response.arrayBuffer());
}
