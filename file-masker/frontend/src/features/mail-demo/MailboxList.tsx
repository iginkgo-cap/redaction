import { useEffect, useRef, useState } from 'react';
import {
  completeOAuth,
  disconnectMailbox,
  getOAuthStatus,
  startOAuth,
  type MailboxInfo,
  type OAuthFlow,
} from './lib/api';

/** 授权进度轮询间隔（设计 §7.2）。 */
export const OAUTH_POLL_MS = 3000;

const PROVIDER_LABEL: Record<MailboxInfo['provider'], string> = { imap: 'IMAP', gmail: 'Gmail', graph: 'Microsoft 365' };
const SINK_LABEL: Record<MailboxInfo['sink'], string> = { fs: 'Folder', smtp: 'Forward' };
const ERROR_LABEL: Record<string, string> = {
  api_unauthorized: 'the mail API rejected the access token (authorize again if it persists)',
};

export function time(iso: string | null): string {
  return iso ? new Date(iso).toLocaleTimeString('en-GB') : '—';
}

function authText(box: MailboxInfo): string {
  switch (box.auth.state) {
    case 'password':
      return 'Password';
    case 'authorized':
      return `Authorized as ${box.auth.account ?? box.address}`;
    case 'expired':
      return 'Authorization expired — authorize again';
    default:
      return 'Not authorized';
  }
}

export function disconnectMessage(address: string): string {
  return (
    `Disconnect ${address}?\n\n` +
    'file-masker will stop reading this mailbox until it is authorized again. ' +
    'The permission granted in the Google / Microsoft account itself stays in place; remove it there if you want it revoked.'
  );
}

type PanelState =
  | { kind: 'starting' }
  | { kind: 'pending'; flow: OAuthFlow }
  | { kind: 'done' }
  | { kind: 'expired' }
  | { kind: 'error'; message: string };

interface PanelProps {
  address: string;
  onDone: () => void;
  onClose: () => void;
}

/** 授权面板：Microsoft 设备码 / Google 链接 + 粘贴；每 3 秒轮询进度；过期提示重试（E4）。 */
function AuthPanel({ address, onDone, onClose }: PanelProps) {
  const [state, setState] = useState<PanelState>({ kind: 'starting' });
  const [pasted, setPasted] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const onDoneRef = useRef(onDone); // 轮询定时器只依赖 flow，父组件重渲染不重置它
  onDoneRef.current = onDone;

  useEffect(() => {
    let alive = true;
    startOAuth(address)
      .then((flow) => alive && setState({ kind: 'pending', flow }))
      .catch((e: Error) => alive && setState({ kind: 'error', message: e.message }));
    return () => {
      alive = false;
    };
  }, [address]);

  const flow = state.kind === 'pending' ? state.flow : null;
  useEffect(() => {
    if (!flow) return;
    let alive = true;
    const poll = () => {
      if (Date.parse(flow.expiresAt) <= Date.now()) {
        setState({ kind: 'expired' });
        return;
      }
      getOAuthStatus(flow.flowId)
        .then((s) => {
          if (!alive) return;
          if (s.status === 'done') {
            setState({ kind: 'done' });
            onDoneRef.current();
          } else if (s.status === 'error') {
            setState(s.message === 'expired' ? { kind: 'expired' } : { kind: 'error', message: s.message });
          }
        })
        .catch((e: Error) => alive && setState({ kind: 'error', message: e.message }));
    };
    const timer = setInterval(poll, OAUTH_POLL_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [flow]);

  async function submitPasted() {
    if (!flow) return;
    setSubmitting(true);
    try {
      const s = await completeOAuth(flow.flowId, pasted.trim());
      if (s.status === 'done') {
        setState({ kind: 'done' });
        onDone();
      } else if (s.status === 'error') {
        setState({ kind: 'error', message: s.message });
      }
    } catch (e) {
      setState({ kind: 'error', message: e instanceof Error ? e.message : 'Request failed.' });
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="mail-auth-panel" role="dialog" aria-label={`Authorize ${address}`}>
      <div className="mail-card-header">
        <h3>Authorize {address}</h3>
        <button type="button" className="mail-auth-close" onClick={onClose}>
          Close
        </button>
      </div>
      {state.kind === 'starting' && <p className="mail-empty">Starting…</p>}
      {flow && flow.mode === 'device' && (
        <>
          <p>
            Open{' '}
            <a href={flow.verificationUri ?? '#'} target="_blank" rel="noreferrer noopener">
              {flow.verificationUri}
            </a>{' '}
            and enter this code, signing in as <strong>{address}</strong>:
          </p>
          <p className="mail-auth-code">{flow.userCode}</p>
          <p className="mail-empty">Waiting for you to finish in the browser…</p>
        </>
      )}
      {flow && flow.mode === 'code' && (
        <>
          <p>
            <a href={flow.authUrl ?? '#'} target="_blank" rel="noreferrer noopener">
              Open the Google sign-in page
            </a>{' '}
            and sign in as <strong>{address}</strong>. If the page you land on afterwards cannot be reached, paste its full
            address here:
          </p>
          <div className="mail-auth-paste">
            <input
              aria-label="Redirected URL"
              placeholder="http://localhost:18090/api/mail/oauth/callback?state=…&code=…"
              value={pasted}
              disabled={submitting}
              onChange={(e) => setPasted(e.target.value)}
            />
            <button type="button" disabled={submitting || pasted.trim() === ''} onClick={submitPasted}>
              Complete
            </button>
          </div>
          <p className="mail-empty">Waiting for the sign-in to finish…</p>
        </>
      )}
      {state.kind === 'done' && <p className="status ok">Authorized. The mailbox will be checked on the next poll.</p>}
      {state.kind === 'expired' && (
        <p className="status error" role="alert">
          This authorization attempt expired (10 minutes). Close and click Authorize again.
        </p>
      )}
      {state.kind === 'error' && (
        <p className="status error" role="alert">
          {state.message}
        </p>
      )}
    </div>
  );
}

interface Props {
  mailboxes: MailboxInfo[];
  /** 授权 / 断开后让页面立即刷新状态。 */
  onChanged: () => void;
  confirm?: (message: string) => boolean;
}

/** 邮箱表 + 授权面板 + Disconnect 确认（设计 §7.1）。 */
export default function MailboxList({ mailboxes, onChanged, confirm = (m) => window.confirm(m) }: Props) {
  const [authorizing, setAuthorizing] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function disconnect(address: string) {
    if (!confirm(disconnectMessage(address))) return;
    setBusy(address);
    setError(null);
    try {
      await disconnectMailbox(address);
      if (authorizing === address) setAuthorizing(null);
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Request failed.');
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="mail-card" aria-label="Mailboxes">
      <div className="mail-card-header">
        <h2>Mailboxes</h2>
      </div>
      <table className="mail-mailboxes">
        <thead>
          <tr>
            <th>Mailbox</th>
            <th>Access</th>
            <th>Output</th>
            <th>Authorization</th>
            <th>Last check</th>
            <th aria-label="Actions" />
          </tr>
        </thead>
        <tbody>
          {mailboxes.map((box) => (
            <tr key={box.address} className={`auth-${box.auth.state}`}>
              <td className="mail-mailbox-address">{box.address}</td>
              <td>{PROVIDER_LABEL[box.provider]}</td>
              <td>{SINK_LABEL[box.sink]}</td>
              <td>{authText(box)}</td>
              <td>
                {time(box.lastPollAt)}
                {box.backfill && <span className="mail-backfill"> · catching up from {box.backfill.slice(0, 10)}</span>}
                {box.lastError && (
                  <span className="mail-mailbox-error"> · {ERROR_LABEL[box.lastError] ?? box.lastError}</span>
                )}
              </td>
              <td className="mail-mailbox-actions">
                {box.auth.state !== 'password' && box.auth.state !== 'authorized' && (
                  <button type="button" disabled={busy !== null} onClick={() => setAuthorizing(box.address)}>
                    Authorize
                  </button>
                )}
                {(box.auth.state === 'authorized' || box.auth.state === 'expired') && (
                  <button
                    type="button"
                    className="mail-disconnect"
                    disabled={busy !== null}
                    onClick={() => disconnect(box.address)}
                  >
                    Disconnect
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {error && (
        <p className="status error" role="alert">
          {error}
        </p>
      )}
      {authorizing && (
        <AuthPanel key={authorizing} address={authorizing} onDone={onChanged} onClose={() => setAuthorizing(null)} />
      )}
    </section>
  );
}
