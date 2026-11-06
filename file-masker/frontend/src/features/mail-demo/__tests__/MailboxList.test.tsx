import { act } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { byButtonText, byLabel, click, flush, renderComponent, type } from '../../../testing/renderComponent';
import type { MailboxInfo } from '../lib/api';
import MailboxList, { OAUTH_POLL_MS, disconnectMessage } from '../MailboxList';

function box(patch: Partial<MailboxInfo> = {}): MailboxInfo {
  return {
    address: 'lq-ops@example.com',
    provider: 'gmail',
    sink: 'fs',
    auth: { state: 'not_authorized', account: null, grantedAt: null },
    lastPollAt: '2026-10-20T10:00:30+08:00',
    lastError: null,
    backfill: null,
    ...patch,
  };
}

type Reply = { ok?: boolean; status?: number; body?: unknown };
let replies: Reply[] = [];
let calls: [string, RequestInit | undefined][] = [];

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] });
  vi.setSystemTime(new Date('2026-10-20T10:01:00+08:00'));
  calls = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push([url, init]);
      const reply = replies.shift() ?? {};
      return { ok: reply.ok ?? true, status: reply.status ?? (reply.ok === false ? 400 : 200), json: async () => reply.body };
    }),
  );
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function tick() {
  await act(async () => {
    vi.advanceTimersByTime(OAUTH_POLL_MS);
  });
  await flush();
}

describe('MailboxList (LOOK-6764)', () => {
  it('shows one row per mailbox with its provider, output, authorization and last check', async () => {
    const r = await renderComponent(
      <MailboxList
        mailboxes={[
          box(),
          box({ address: 'ops@look.example', provider: 'graph', auth: { state: 'authorized', account: 'ops@look.example', grantedAt: 'x' }, backfill: '2026-10-14T00:00:00+00:00' }),
          box({ address: 'demo@example.com', provider: 'imap', sink: 'smtp', auth: { state: 'password', account: null, grantedAt: null }, lastError: 'OSError: refused' }),
          box({ address: 'old@example.com', auth: { state: 'expired', account: 'old@example.com', grantedAt: 'x' }, lastError: 'api_unauthorized' }),
        ]}
        onChanged={() => {}}
      />,
    );
    const rows = r.all('tbody tr').map((tr) => [...tr.querySelectorAll('td')].map((td) => td.textContent!.trim()));
    // 页面按浏览器本地时区显示时间；期望值跟着运行环境的时区算，CI（UTC）与本机（UTC+8）都成立
    const lastPoll = new Date('2026-10-20T10:00:30+08:00').toLocaleTimeString('en-GB');
    expect(lastPoll).toMatch(/^\d{2}:00:30$/);
    expect(rows[0].slice(0, 5)).toEqual(['lq-ops@example.com', 'Gmail', 'Folder', 'Not authorized', lastPoll]);
    expect(rows[1].slice(0, 4)).toEqual(['ops@look.example', 'Microsoft 365', 'Folder', 'Authorized as ops@look.example']);
    expect(rows[1][4]).toContain('catching up from 2026-10-14');
    expect(rows[2].slice(0, 4)).toEqual(['demo@example.com', 'IMAP', 'Forward', 'Password']);
    expect(rows[2][4]).toContain('· OSError: refused');
    expect(rows[3][3]).toBe('Authorization expired — authorize again');
    expect(rows[3][4]).toContain('rejected the access token');
    expect(r.all('button').map((b) => b.textContent)).toEqual(['Authorize', 'Disconnect', 'Authorize', 'Disconnect']);
  });

  it('runs the Microsoft device-code flow and polls until done', async () => {
    const onChanged = vi.fn();
    const r = await renderComponent(<MailboxList mailboxes={[box({ address: 'ops@look.example', provider: 'graph' })]} onChanged={onChanged} />);
    replies = [
      { body: { flowId: 'f1', mode: 'device', authUrl: null, userCode: 'ABCD-EFGH', verificationUri: 'https://microsoft.com/devicelogin', expiresAt: '2026-10-20T10:11:00+08:00' } },
      { body: { status: 'pending', message: '' } },
      { body: { status: 'done', message: '' } },
    ];
    await click(byButtonText(r, 'Authorize'));
    await flush();
    expect(calls[0][0]).toBe('api/mail/mailboxes/ops%40look.example/oauth');
    expect(calls[0][1]?.method).toBe('POST');
    expect(r.text()).toContain('ABCD-EFGH');
    expect(r.find('a')!.getAttribute('href')).toBe('https://microsoft.com/devicelogin');
    expect(r.text()).toContain('signing in as ops@look.example');

    await tick();
    expect(calls[1][0]).toBe('api/mail/oauth/f1');
    expect(onChanged).not.toHaveBeenCalled();
    await tick();
    expect(r.text()).toContain('Authorized. The mailbox will be checked on the next poll.');
    expect(onChanged).toHaveBeenCalledTimes(1);
    await tick();
    expect(calls).toHaveLength(3); // 完成后停止轮询
  });

  it('runs the Google flow with a link and the pasted-URL fallback', async () => {
    const onChanged = vi.fn();
    const r = await renderComponent(<MailboxList mailboxes={[box()]} onChanged={onChanged} />);
    replies = [
      { body: { flowId: 'f2', mode: 'code', authUrl: 'https://accounts.google.com/o/oauth2/v2/auth?state=abc', userCode: null, verificationUri: null, expiresAt: '2026-10-20T10:11:00+08:00' } },
      { body: { status: 'done', message: '' } },
    ];
    await click(byButtonText(r, 'Authorize'));
    await flush();
    expect(r.find('a')!.getAttribute('href')).toBe('https://accounts.google.com/o/oauth2/v2/auth?state=abc');
    expect(byButtonText(r, 'Complete')!.disabled).toBe(true);
    await type(byLabel(r, 'Redirected URL'), ' http://localhost:18090/api/mail/oauth/callback?state=abc&code=xyz ');
    await click(byButtonText(r, 'Complete'));
    await flush();
    expect(calls[1][0]).toBe('api/mail/oauth/f2/complete');
    expect(JSON.parse(calls[1][1]!.body as string)).toEqual({ redirectedUrl: 'http://localhost:18090/api/mail/oauth/callback?state=abc&code=xyz' });
    expect(r.text()).toContain('Authorized.');
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it('shows the server message when the account does not match', async () => {
    const r = await renderComponent(<MailboxList mailboxes={[box()]} onChanged={() => {}} />);
    replies = [
      { body: { flowId: 'f3', mode: 'code', authUrl: 'https://g/x', userCode: null, verificationUri: null, expiresAt: '2026-10-20T10:11:00+08:00' } },
      { body: { status: 'error', message: 'authorized someone-else@example.com, but this mailbox is lq-ops@example.com' } },
    ];
    await click(byButtonText(r, 'Authorize'));
    await flush();
    await tick();
    expect(r.find('[role="alert"]')!.textContent).toContain('someone-else@example.com');
  });

  it('tells the user when the attempt expired', async () => {
    const r = await renderComponent(<MailboxList mailboxes={[box()]} onChanged={() => {}} />);
    replies = [
      { body: { flowId: 'f4', mode: 'code', authUrl: 'https://g/x', userCode: null, verificationUri: null, expiresAt: '2026-10-20T10:01:02+08:00' } },
    ];
    await click(byButtonText(r, 'Authorize'));
    await flush();
    await tick(); // 3 秒后已过 expiresAt：不再请求，直接提示
    expect(calls).toHaveLength(1);
    expect(r.find('[role="alert"]')!.textContent).toContain('expired');
  });

  it('shows the error when authorization cannot start', async () => {
    const r = await renderComponent(<MailboxList mailboxes={[box()]} onChanged={() => {}} />);
    replies = [{ ok: false, body: { error: 'device code request failed: HTTP 503' } }];
    await click(byButtonText(r, 'Authorize'));
    await flush();
    expect(r.find('[role="alert"]')!.textContent).toContain('HTTP 503');
  });

  it('disconnects only after confirmation and explains that the cloud grant stays', async () => {
    const onChanged = vi.fn();
    const confirm = vi.fn(() => false);
    const authorized = box({ auth: { state: 'authorized', account: 'lq-ops@example.com', grantedAt: 'x' } });
    const r = await renderComponent(<MailboxList mailboxes={[authorized]} onChanged={onChanged} confirm={confirm} />);
    await click(byButtonText(r, 'Disconnect'));
    await flush();
    expect(confirm).toHaveBeenCalledWith(disconnectMessage('lq-ops@example.com'));
    expect(disconnectMessage('lq-ops@example.com')).toContain('stays in place');
    expect(calls).toHaveLength(0);

    confirm.mockReturnValue(true);
    replies = [{ status: 204 }];
    await click(byButtonText(r, 'Disconnect'));
    await flush();
    expect(calls[0][0]).toBe('api/mail/mailboxes/lq-ops%40example.com/oauth');
    expect(calls[0][1]?.method).toBe('DELETE');
    expect(onChanged).toHaveBeenCalledTimes(1);
  });
});
