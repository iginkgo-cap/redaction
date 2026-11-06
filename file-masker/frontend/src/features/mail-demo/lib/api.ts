/** 邮件模式页接口（LOOK-6764，设计 §6）。规则按租户存在服务端 SQLite，与 PDF 打码页的规则互不相通。 */
export interface MailRule {
  source: string;
  target: string;
}

export type AuthState = 'password' | 'authorized' | 'not_authorized' | 'expired';

export interface MailboxAuth {
  state: AuthState;
  account: string | null;
  grantedAt: string | null;
}

export interface MailboxInfo {
  address: string;
  provider: 'imap' | 'gmail' | 'graph';
  sink: 'fs' | 'smtp';
  auth: MailboxAuth;
  lastPollAt: string | null;
  lastError: string | null;
  /** 回溯中：当前扫描检查点；追平后为 null。 */
  backfill: string | null;
}

export interface MailRecord {
  at: string;
  /** 来源邮箱地址。 */
  source: string;
  /** 打码后的标题；扣留且无法得到打码标题时为空。 */
  subject: string;
  status: 'written' | 'sent' | 'held';
  reason: string;
  hits: number;
  attachments: number;
  /** written：落盘目录；sent：目标邮箱。 */
  location: string;
  /** 没能打码、没带上的附件数。 */
  skipped: number;
  /** 整封原件的存放路径；没存为空。 */
  original: string;
}

export interface TenantStatus {
  code: string;
  mailboxes: MailboxInfo[];
  rules: MailRule[];
  records: MailRecord[];
}

export interface MailStatus {
  pollSeconds: number;
  startedAt: string;
  tenants: TenantStatus[];
}

export interface OAuthFlow {
  flowId: string;
  mode: 'device' | 'code';
  authUrl: string | null;
  userCode: string | null;
  verificationUri: string | null;
  expiresAt: string;
}

export interface OAuthFlowStatus {
  status: 'pending' | 'done' | 'error';
  message: string;
}

/** 服务端返回的英文错误原因，可以直接展示给用户。 */
export class MailRequestError extends Error {}

async function errorOf(response: Response): Promise<MailRequestError> {
  const payload = (await response.json().catch(() => null)) as { error?: string } | null;
  return new MailRequestError(payload?.error ?? `Request failed (HTTP ${response.status}).`);
}

async function call<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) throw await errorOf(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function json(method: string, body?: unknown): RequestInit {
  return { method, headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) };
}

function mailboxPath(address: string): string {
  return `api/mail/mailboxes/${encodeURIComponent(address)}/oauth`;
}

export function fetchMailStatus(): Promise<MailStatus> {
  return call<MailStatus>('api/mail');
}

export async function saveTenantRules(tenant: string, rules: MailRule[]): Promise<MailRule[]> {
  const result = await call<{ rules: MailRule[] }>(`api/mail/tenants/${encodeURIComponent(tenant)}/rules`, json('PUT', rules));
  return result.rules;
}

export function startOAuth(address: string): Promise<OAuthFlow> {
  return call<OAuthFlow>(mailboxPath(address), { method: 'POST' });
}

export function getOAuthStatus(flowId: string): Promise<OAuthFlowStatus> {
  return call<OAuthFlowStatus>(`api/mail/oauth/${encodeURIComponent(flowId)}`);
}

export function completeOAuth(flowId: string, redirectedUrl: string): Promise<OAuthFlowStatus> {
  return call<OAuthFlowStatus>(`api/mail/oauth/${encodeURIComponent(flowId)}/complete`, json('POST', { redirectedUrl }));
}

export function disconnectMailbox(address: string): Promise<void> {
  return call<void>(mailboxPath(address), { method: 'DELETE' });
}
