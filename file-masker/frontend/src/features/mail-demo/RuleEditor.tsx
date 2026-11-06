import { useEffect, useState } from 'react';
import { CloseIcon, PlusIcon } from '../../components/icons';
import { saveTenantRules, type MailRule } from './lib/api';

/** 与服务端 `MAX_RULES` 一致。 */
const MAX_RULES = 100;

interface Row extends MailRule {
  id: number;
}

let rowSeq = 0;
function toRows(rules: MailRule[]): Row[] {
  const rows = rules.map((r) => ({ ...r, id: ++rowSeq }));
  return rows.length > 0 ? rows : [{ source: '', target: '', id: ++rowSeq }];
}

/** 原文为空的行不提交：新加的空行不该让保存报错。 */
function normalize(rows: Row[]): MailRule[] {
  return rows
    .filter((r) => r.source.trim() !== '')
    .map((r) => ({ source: r.source.trim(), target: r.target }));
}

interface Props {
  /** 规则属于哪个租户：保存到 `PUT /api/mail/tenants/{tenant}/rules`。 */
  tenant: string;
  initialRules: MailRule[];
  /** 有未保存修改时通知页面（切换租户页签前要确认）。 */
  onDirtyChange?: (dirty: boolean) => void;
}

/**
 * 邮件页的打码规则编辑器（LOOK-6764）。独立于 PDF 打码页的 KeywordRows：
 * 规则按租户存在服务端、点 Save 才生效，不读写 localStorage。
 * `initialRules` 只在挂载时读取一次，之后页面的定时刷新不会覆盖正在编辑的内容。
 * 保存期间锁住编辑：返回后会用服务端结果重置规则行。
 */
export default function RuleEditor({ tenant, initialRules, onDirtyChange }: Props) {
  const [saved, setSaved] = useState<MailRule[]>(initialRules);
  const [rows, setRows] = useState<Row[]>(() => toRows(initialRules));
  const [saving, setSaving] = useState(false);
  const [justSaved, setJustSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const dirty = JSON.stringify(normalize(rows)) !== JSON.stringify(saved);
  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  function edit(next: Row[]) {
    setRows(next);
    setJustSaved(false);
    setError(null);
  }

  function update(id: number, patch: Partial<MailRule>) {
    edit(rows.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  }

  async function save() {
    setSaving(true);
    setError(null);
    try {
      const result = await saveTenantRules(tenant, normalize(rows));
      setSaved(result);
      setRows(toRows(result));
      setJustSaved(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Saving failed.');
    } finally {
      setSaving(false);
    }
  }

  const state = saving ? 'Saving…' : dirty ? 'Unsaved changes' : justSaved ? 'Saved' : '';

  return (
    <section className="mail-card" aria-label="Masking rules">
      <div className="mail-card-header">
        <h2>Masking rules</h2>
        <span className="mail-rules-state">{state}</span>
        <button type="button" onClick={save} disabled={!dirty || saving}>
          Save
        </button>
      </div>
      {saved.length === 0 && <p className="status error">No rules — incoming emails for {tenant} will be held.</p>}
      <div className="mail-rule-list">
        {rows.map((row, i) => (
          <div className="mail-rule-row" key={row.id}>
            <input
              aria-label={`Text to mask ${i + 1}`}
              placeholder="Text to mask"
              disabled={saving}
              value={row.source}
              onChange={(e) => update(row.id, { source: e.target.value })}
            />
            <span className="mail-rule-arrow" aria-hidden="true">
              →
            </span>
            <input
              aria-label={`Replace with ${i + 1}`}
              placeholder="Replace with (empty = delete)"
              disabled={saving}
              value={row.target}
              onChange={(e) => update(row.id, { target: e.target.value })}
            />
            <button
              type="button"
              className="mail-rule-remove"
              aria-label={`Remove rule ${i + 1}`}
              disabled={saving}
              onClick={() => edit(rows.length > 1 ? rows.filter((r) => r.id !== row.id) : toRows([]))}
            >
              <CloseIcon size={14} />
            </button>
          </div>
        ))}
      </div>
      <button
        type="button"
        className="mail-rule-add"
        disabled={saving || rows.length >= MAX_RULES}
        onClick={() => edit([...rows, { source: '', target: '', id: ++rowSeq }])}
      >
        <PlusIcon size={14} />
        Add rule
      </button>
      {error && (
        <p className="status error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
