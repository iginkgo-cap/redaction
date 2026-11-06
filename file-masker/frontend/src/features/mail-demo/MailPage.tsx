import { useCallback, useEffect, useRef, useState } from 'react';
import Activity from './Activity';
import { fetchMailStatus, type MailStatus } from './lib/api';
import MailboxList from './MailboxList';
import RuleEditor from './RuleEditor';

export const REFRESH_MS = 5000;

/**
 * 邮件模式页（LOOK-6764）：租户页签 → 该租户的邮箱（含授权）、规则、处理记录。
 * 邮箱来自服务端配置文件，只读；规则按租户在本页配置；状态每 5 秒刷新，不覆盖正在编辑的规则。
 */
export default function MailPage({ confirm = (m) => window.confirm(m) }: { confirm?: (message: string) => boolean }) {
  const [status, setStatus] = useState<MailStatus | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [active, setActive] = useState<string | null>(null);
  const dirty = useRef(false);

  const load = useCallback(() => {
    return fetchMailStatus()
      .then((s) => {
        setStatus(s);
        setLoadError(null);
      })
      .catch((e: Error) => setLoadError(e.message));
  }, []);

  useEffect(() => {
    let alive = true;
    const run = () => {
      if (alive) void load();
    };
    run();
    const timer = setInterval(run, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [load]);

  const onDirtyChange = useCallback((d: boolean) => {
    dirty.current = d;
  }, []);
  const onChanged = useCallback(() => {
    void load();
  }, [load]);

  if (!status) {
    return (
      <div className="mail-demo">
        {loadError ? <p className="status error">{loadError}</p> : <p className="mail-empty">Loading…</p>}
      </div>
    );
  }

  const tenant = status.tenants.find((t) => t.code === active) ?? status.tenants[0];
  if (!tenant) {
    return (
      <div className="mail-demo">
        <p className="mail-empty">No tenants configured.</p>
      </div>
    );
  }

  function switchTo(code: string) {
    if (code === tenant!.code) return;
    if (dirty.current && !confirm('Masking rules have unsaved changes. Discard them and switch tenant?')) return;
    dirty.current = false;
    setActive(code);
  }

  return (
    <div className="mail-demo">
      <nav className="mail-tabs" aria-label="Tenants">
        {status.tenants.map((t) => (
          <button
            type="button"
            key={t.code}
            className={`mail-tab${t.code === tenant.code ? ' active' : ''}`}
            aria-pressed={t.code === tenant.code}
            onClick={() => switchTo(t.code)}
          >
            {t.code}
          </button>
        ))}
        <span className="mail-flow-meta">Checking every {status.pollSeconds}s</span>
      </nav>
      {loadError && <p className="status error">Refresh failed: {loadError}</p>}

      {/* 以租户为 key：切换页签时关掉上一个租户打开的授权面板 */}
      <MailboxList key={`mailboxes-${tenant.code}`} mailboxes={tenant.mailboxes} onChanged={onChanged} confirm={confirm} />

      {/* 规则只在挂载时读取，刷新不覆盖正在编辑的内容；切换租户时重新挂载 */}
      <RuleEditor key={`rules-${tenant.code}`} tenant={tenant.code} initialRules={tenant.rules} onDirtyChange={onDirtyChange} />

      <Activity records={tenant.records} showSource={tenant.mailboxes.length > 1} />
    </div>
  );
}
