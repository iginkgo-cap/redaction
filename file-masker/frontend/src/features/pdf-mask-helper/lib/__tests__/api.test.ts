import { afterEach, describe, expect, it, vi } from 'vitest';
import { maskPdf, MaskRequestError } from '../api';

afterEach(() => vi.restoreAllMocks());

describe('maskPdf (local /mask)', () => {
  it('posts the file and keywords as multipart and returns the masked bytes', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(new Uint8Array([37, 80, 68, 70]), { status: 200 }),
    );
    const file = new File([new Uint8Array([1, 2, 3])], 'a.pdf', { type: 'application/pdf' });
    const bytes = await maskPdf(file, [{ source: 'Zhang San', target: '***' }]);
    expect(Array.from(bytes)).toEqual([37, 80, 68, 70]);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('mask');
    const body = (init as RequestInit).body as FormData;
    expect((body.get('file') as File).name).toBe('a.pdf');
    expect(JSON.parse(body.get('keywords') as string)).toEqual([{ source: 'Zhang San', target: '***' }]);
  });

  it('throws MaskRequestError carrying the server message', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ error: 'Page 2 is a scanned image with no text layer and cannot be masked.' }), { status: 422 }),
    );
    const file = new File([new Uint8Array([1])], 'b.pdf');
    await expect(maskPdf(file, [{ source: 'abc', target: '' }])).rejects.toThrow(/scanned image/);
  });

  it('falls back to the HTTP status when the body is not JSON', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('oops', { status: 502 }));
    const err = await maskPdf(new File([new Uint8Array([1])], 'c.pdf'), [{ source: 'abc', target: '' }]).catch((e) => e);
    expect(err).toBeInstanceOf(MaskRequestError);
    expect(err.message).toBe('Masking failed (HTTP 502).');
  });
});
