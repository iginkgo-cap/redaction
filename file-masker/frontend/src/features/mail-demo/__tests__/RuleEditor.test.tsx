import { afterEach, describe, expect, it, vi } from 'vitest';
import { byButtonText, byLabel, click, flush, renderComponent, type } from '../../../testing/renderComponent';
import RuleEditor from '../RuleEditor';

function mockFetch(response: { ok: boolean; status?: number; body: unknown }) {
  const fn = vi.fn(async () => ({
    ok: response.ok,
    status: response.status ?? (response.ok ? 200 : 400),
    json: async () => response.body,
  }));
  vi.stubGlobal('fetch', fn);
  return fn;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('RuleEditor (LOOK-6764)', () => {
  it('shows the no-rules warning and one blank row when nothing is saved', async () => {
    const r = await renderComponent(<RuleEditor tenant="lq" initialRules={[]} />);
    expect(r.text()).toContain('No rules — incoming emails for lq will be held.');
    expect(r.all('input')).toHaveLength(2);
    expect(byButtonText(r, 'Save')!.disabled).toBe(true);
  });

  it('marks unsaved changes and saves trimmed rules with PUT', async () => {
    const fetchFn = mockFetch({ ok: true, body: { rules: [{ source: 'Zhang San', target: 'CLIENT-A' }] } });
    const r = await renderComponent(<RuleEditor tenant="lq" initialRules={[]} />);
    await type(byLabel(r, 'Text to mask 1'), '  Zhang San ');
    await type(byLabel(r, 'Replace with 1'), 'CLIENT-A');
    expect(r.text()).toContain('Unsaved changes');

    await click(byButtonText(r, 'Save'));
    await flush();

    expect(fetchFn).toHaveBeenCalledWith('api/mail/tenants/lq/rules', expect.objectContaining({ method: 'PUT' }));
    const body = JSON.parse((fetchFn.mock.calls[0] as unknown as [string, RequestInit])[1].body as string);
    expect(body).toEqual([{ source: 'Zhang San', target: 'CLIENT-A' }]);
    expect(r.text()).toContain('Saved');
    expect(r.text()).not.toContain('Unsaved changes');
    expect(r.text()).not.toContain('No rules');
  });

  it('ignores rows without text to mask', async () => {
    const fetchFn = mockFetch({ ok: true, body: { rules: [{ source: 'ACME Ltd', target: 'CO-B' }] } });
    const r = await renderComponent(<RuleEditor tenant="lq" initialRules={[{ source: 'ACME Ltd', target: 'CO-A' }]} />);
    await click(byButtonText(r, 'Add rule'));
    await type(byLabel(r, 'Replace with 2'), 'ignored');
    expect(byButtonText(r, 'Save')!.disabled).toBe(true); // 只多了空原文的行，不算改动

    await type(byLabel(r, 'Replace with 1'), 'CO-B');
    await click(byButtonText(r, 'Save'));
    await flush();
    const body = JSON.parse((fetchFn.mock.calls[0] as unknown as [string, RequestInit])[1].body as string);
    expect(body).toEqual([{ source: 'ACME Ltd', target: 'CO-B' }]);
  });

  it('removing every rule and saving clears them', async () => {
    const fetchFn = mockFetch({ ok: true, body: { rules: [] } });
    const r = await renderComponent(<RuleEditor tenant="lq" initialRules={[{ source: 'ACME Ltd', target: 'CO-A' }]} />);
    await click(byLabel(r, 'Remove rule 1'));
    await click(byButtonText(r, 'Save'));
    await flush();
    const body = JSON.parse((fetchFn.mock.calls[0] as unknown as [string, RequestInit])[1].body as string);
    expect(body).toEqual([]);
    expect(r.text()).toContain('No rules — incoming emails for lq will be held.');
  });

  it('locks the editor while a save is in flight', async () => {
    let finish: (v: unknown) => void = () => {};
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise((resolve) => {
        finish = resolve;
      })),
    );
    const r = await renderComponent(<RuleEditor tenant="lq" initialRules={[]} />);
    await type(byLabel(r, 'Text to mask 1'), 'Zhang San');
    await click(byButtonText(r, 'Save'));

    // 保存返回时会用服务端结果重置规则行，保存期间的输入会丢，所以先锁住
    expect(r.text()).toContain('Saving…');
    expect(r.all<HTMLInputElement>('input').every((i) => i.disabled)).toBe(true);
    expect(byButtonText(r, 'Add rule')!.disabled).toBe(true);
    expect(byLabel<HTMLButtonElement>(r, 'Remove rule 1')!.disabled).toBe(true);

    finish({ ok: true, status: 200, json: async () => ({ rules: [{ source: 'Zhang San', target: '' }] }) });
    await flush();
    await flush();
    expect(r.all<HTMLInputElement>('input').every((i) => !i.disabled)).toBe(true);
  });

  it('reports dirty state to the page', async () => {
    const onDirtyChange = vi.fn();
    mockFetch({ ok: true, body: { rules: [{ source: 'Zhang San', target: '' }] } });
    const r = await renderComponent(<RuleEditor tenant="lq" initialRules={[]} onDirtyChange={onDirtyChange} />);
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
    await type(byLabel(r, 'Text to mask 1'), 'Zhang San');
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);
    await click(byButtonText(r, 'Save'));
    await flush();
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
  });

  it('shows the server reason when saving fails and keeps the edit', async () => {
    mockFetch({ ok: false, body: { error: '"Li": text to mask needs at least 3 characters (2 for Chinese).' } });
    const r = await renderComponent(<RuleEditor tenant="lq" initialRules={[]} />);
    await type(byLabel(r, 'Text to mask 1'), 'Li');
    await click(byButtonText(r, 'Save'));
    await flush();
    expect(r.find('[role="alert"]')!.textContent).toContain('at least 3 characters');
    expect(byLabel<HTMLInputElement>(r, 'Text to mask 1')!.value).toBe('Li');
    expect(r.text()).toContain('Unsaved changes');
  });
});
