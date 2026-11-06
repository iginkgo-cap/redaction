import { act } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { byButtonText, byLabel, click, flush, renderComponent, type } from '../../../testing/renderComponent';
import type { MailRecord, MailStatus, TenantStatus } from '../lib/api';
import { detail } from '../Activity';
import MailPage, { REFRESH_MS } from '../MailPage';

function tenant(code: string, patch: Partial<TenantStatus> = {}): TenantStatus {
  return {
    code,
    mailboxes: [
      {
        address: `${code}-ops@example.com`,
        provider: 'gmail',
        sink: 'fs',
        auth: { state: 'authorized', account: `${code}-ops@example.com`, grantedAt: 'x' },
        lastPollAt: '2026-10-20T10:00:30+08:00',
        lastError: null,
        backfill: null,
      },
    ],
    rules: [],
    records: [],
    ...patch,
  };
}

function status(tenants: TenantStatus[] = [tenant('lq'), tenant('look')]): MailStatus {
  return { pollSeconds: 30, startedAt: '2026-10-20T09:00:00+08:00', tenants };
}

function record(patch: Partial<MailRecord>): MailRecord {
  return { at: '2026-10-20T10:02:01+08:00', source: 'lq-ops@example.com', subject: '', status: 'held', reason: '', hits: 0, attachments: 0, location: '', skipped: 0, original: '', ...patch };
}

let responses: MailStatus[] = [];

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: true, status: 200, json: async () => responses.shift() ?? status() })),
  );
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function render(confirm?: (m: string) => boolean) {
  const r = await renderComponent(<MailPage confirm={confirm} />);
  await flush();
  return r;
}

async function refresh() {
  await act(async () => {
    vi.advanceTimersByTime(REFRESH_MS);
  });
  await flush();
}

describe('MailPage (LOOK-6764)', () => {
  it('shows tenant tabs, the first tenant and all three record kinds', async () => {
    responses = [
      status([
        tenant('lq', {
          rules: [{ source: 'Zhang San', target: 'CLIENT-A' }],
          records: [
            record({ reason: 'scanned_pdf' }),
            record({ at: '2026-10-20T10:00:31+08:00', subject: 'CLIENT-A statement', status: 'written', hits: 7, attachments: 1, location: '/srv/masked/lq/x' }),
            record({ at: '2026-10-20T10:00:20+08:00', subject: 'fwd', status: 'sent', hits: 2, attachments: 2, location: 'dst@example.com' }),
          ],
        }),
        tenant('look'),
      ]),
    ];
    const r = await render();
    const text = r.text();
    expect(r.all('.mail-tab').map((b) => b.textContent)).toEqual(['lq', 'look']);
    expect(text).toContain('Checking every 30s');
    expect(text).toContain('lq-ops@example.com');
    expect(text).not.toContain('look-ops@example.com');
    expect(byLabel<HTMLInputElement>(r, 'Text to mask 1')!.value).toBe('Zhang San');
    expect(text).toContain('Held · scanned PDF');
    expect(text).toContain('Written · 7 replacements · 1 attachment');
    expect(text).toContain('Sent · 2 replacements · 2 attachments');
    expect(r.all('.mail-activity-detail')[1].getAttribute('title')).toBe('/srv/masked/lq/x');
    // 记录跨天，时间前带日期。按浏览器本地时区显示，期望的日期跟着运行环境的时区算（UTC-8 下是 19 Oct）
    const day = new Date('2026-10-20T10:02:01+08:00').toLocaleDateString('en-GB', { day: '2-digit', month: 'short' });
    expect(r.all('.mail-activity-time')[0].textContent).toMatch(new RegExp(`^${day}, \\d{2}:\\d{2}:\\d{2}$`));
    expect(r.all('.mail-activity-source')).toHaveLength(0); // 单邮箱不显示来源列
  });

  it('says when attachments were skipped and the original was kept', () => {
    const written = record({ status: 'written', hits: 1, attachments: 1, skipped: 1, original: '/srv/originals/lq/a.eml' });
    expect(detail(written)).toBe('Written · 1 replacement · 1 attachment · 1 skipped');
    expect(detail(record({ reason: 'no_rules', original: '/srv/originals/lq/b.eml' }))).toBe('Held · no masking rules · original saved');
    expect(detail(record({ reason: 'no_rules' }))).toBe('Held · no masking rules');
  });

  it('switches tenant and shows the source column when a tenant has several mailboxes', async () => {
    const second = tenant('look');
    second.mailboxes = [second.mailboxes[0], { ...second.mailboxes[0], address: 'look-2@example.com' }];
    second.records = [record({ source: 'look-2@example.com', subject: 'x', status: 'written', location: '/p' })];
    responses = [status([tenant('lq'), second])];
    const r = await render();
    await click(byButtonText(r, 'look'));
    const text = r.text();
    expect(text).toContain('look-ops@example.com');
    expect(text).toContain('look-2@example.com');
    expect(r.all('.mail-activity-source')[0].textContent).toBe('look-2@example.com');
    expect(text).toContain('incoming emails for look will be held');
  });

  it('asks before switching tenant with unsaved rules', async () => {
    const confirm = vi.fn(() => false);
    const r = await render(confirm);
    await type(byLabel(r, 'Text to mask 1'), 'Zhang San');
    await click(byButtonText(r, 'look'));
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('unsaved'));
    expect(byLabel<HTMLInputElement>(r, 'Text to mask 1')!.value).toBe('Zhang San'); // 留在原页签
    confirm.mockReturnValue(true);
    await click(byButtonText(r, 'look'));
    expect(byLabel<HTMLInputElement>(r, 'Text to mask 1')!.value).toBe('');
    await click(byButtonText(r, 'lq'));
    expect(confirm).toHaveBeenCalledTimes(2); // 切回去不再问：新页签没有改动
  });

  it('closes an open authorization panel when switching tenant', async () => {
    const lq = tenant('lq');
    lq.mailboxes[0].auth = { state: 'not_authorized', account: null, grantedAt: null };
    responses = [status([lq, tenant('look')])];
    const r = await render();
    responses = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ flowId: 'f', mode: 'code', authUrl: 'https://g/x', userCode: null, verificationUri: null, expiresAt: '2099-01-01T00:00:00Z' }) })),
    );
    await click(byButtonText(r, 'Authorize'));
    await flush();
    expect(r.find('[role="dialog"]')).not.toBeNull();
    await click(byButtonText(r, 'look'));
    expect(r.find('[role="dialog"]')).toBeNull();
  });

  it('refreshes activity without overwriting rules being edited', async () => {
    const r = await render();
    await type(byLabel(r, 'Text to mask 1'), 'Zhang San');
    responses = [status([tenant('lq', { rules: [{ source: 'Other', target: 'X' }], records: [record({ subject: 'new mail', status: 'written', hits: 1, location: '/p' })] }), tenant('look')])];
    await refresh();
    expect(r.text()).toContain('new mail');
    expect(byLabel<HTMLInputElement>(r, 'Text to mask 1')!.value).toBe('Zhang San');
    expect(r.text()).toContain('Unsaved changes');
  });

  it('shows the load error when mail mode is off', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: false, status: 404, json: async () => ({ error: 'Mail mode is not enabled.' }) })),
    );
    const r = await render();
    expect(r.text()).toContain('Mail mode is not enabled.');
  });
});
