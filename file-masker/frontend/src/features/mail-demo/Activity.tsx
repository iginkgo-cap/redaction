import type { MailRecord } from './lib/api';

const HELD_REASONS: Record<string, string> = {
  no_rules: 'no masking rules',
  scanned_pdf: 'scanned PDF',
  invalid_pdf: 'encrypted or damaged PDF',
  unsupported_attachment: 'unsupported attachment',
  bad_archive: 'unsupported zip',
  residual_keyword: 'masked text still found',
  too_large: 'too large',
  send_rejected: 'rejected by mail server',
  decode_error: 'unreadable encoding',
  internal_error: 'internal error',
  dir_conflict: 'output folder already holds another mail',
  own_output: "file-masker's own output",
};

/** 记录跨多天，时间前带日期，如 28 Oct, 14:25:35。 */
function dateTime(iso: string): string {
  return new Date(iso).toLocaleString('en-GB', {
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? '' : 's'}`;
}

export function detail(record: MailRecord): string {
  if (record.status === 'held') {
    const held = `Held · ${HELD_REASONS[record.reason] ?? record.reason}`;
    return record.original ? `${held} · original saved` : held;
  }
  const verb = record.status === 'written' ? 'Written' : 'Sent';
  const text = `${verb} · ${plural(record.hits, 'replacement')} · ${plural(record.attachments, 'attachment')}`;
  return record.skipped ? `${text} · ${record.skipped} skipped` : text;
}

/** 悬停提示：落盘目录与原件路径。 */
function paths(record: MailRecord): string | undefined {
  const lines = [record.status === 'written' ? record.location : '', record.original ? `Original: ${record.original}` : ''];
  return lines.filter(Boolean).join('\n') || undefined;
}

/** 一个租户最近的处理记录；多邮箱时显示来源；悬停显示落盘目录与原件路径。 */
export default function Activity({ records, showSource }: { records: MailRecord[]; showSource: boolean }) {
  return (
    <section className="mail-card" aria-label="Activity">
      <div className="mail-card-header">
        <h2>Activity</h2>
      </div>
      {records.length === 0 ? (
        <p className="mail-empty">No emails processed yet.</p>
      ) : (
        <ul className={`mail-activity${showSource ? ' with-source' : ''}`}>
          {records.map((record, i) => (
            <li key={`${record.at}-${record.source}-${i}`} className={`mail-activity-item ${record.status}`}>
              <span className="mail-activity-time">{dateTime(record.at)}</span>
              {showSource && <span className="mail-activity-source">{record.source}</span>}
              <span className="mail-activity-subject">{record.subject || '(subject not shown)'}</span>
              <span className="mail-activity-detail" title={paths(record)}>
                {detail(record)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
