import { useRef, useState } from 'react';
import { CloseIcon, PlusIcon, ChevronDownIcon } from '../../components/icons';
import AnchoredPanel from '../../lib/overlay/AnchoredPanel';

export interface KeywordRow {
  id: string;
  source: string;
  target: string;
}

interface KeywordRowsProps {
  rows: KeywordRow[];
  onChange: (rows: KeywordRow[]) => void;
  disabled?: boolean;
  /** 规则条数上限，镜像后端 `MaskPdfReqData.keywords` 的 `@Size(max = 100)`。 */
  maxRows: number;
  /** 点了主操作但一条有效规则都没有：给第一行原文框打上 aria-invalid + 红框。 */
  invalid?: boolean;
}

/** 最常用的默认替换值（LOOK-6613）。 */
const DEFAULT_TARGET = '[REDACTED]';
/** 与 `DEFAULT_TARGET` 同一个下拉里的另外两个标准预设。 */
const TARGET_PRESETS = ['[REDACTED]', '[REDACTED_PERSON_NAME]', '[REDACTED_COMPANY_NAME]'];

let rowSeq = 0;
export function newKeywordRow(): KeywordRow {
  rowSeq += 1;
  return { id: `kw-${rowSeq}`, source: '', target: DEFAULT_TARGET };
}

/**
 * 打码规则行：原文 → 替换为（留空 = 删除）。受控组件——父级持有 rows。
 *
 * 「替换为」是可编辑输入框 + 内联预设下拉的合体：自由输入、点选预设、搜索预设都写同一份
 * `row.target`，没有模式切换分支（LOOK-6613 §2.3）。下拉展开时的过滤基准 `filterText` 与
 * `row.target` 分离——展开瞬间重置为空（显示全部预设），此后跟随键入收窄，面板关闭即丢弃。
 */
export default function KeywordRows({ rows, onChange, disabled, maxRows, invalid }: KeywordRowsProps) {
  const atLimit = rows.length >= maxRows;
  const [openRowId, setOpenRowId] = useState<string | null>(null);
  const [filterText, setFilterText] = useState('');
  /** 每行的输入框/触发器 DOM，供打开面板时聚焦/全选、以及给 AnchoredPanel 当锚点。 */
  const inputRefs = useRef(new Map<string, HTMLInputElement>());
  const triggerRefs = useRef(new Map<string, HTMLButtonElement>());
  /**
   * 桥接给 AnchoredPanel 的稳定 ref 对象：`.current` 在渲染时指向当前展开行的触发器按钮
   * ——不能指向 `.mask-target-field`（输入框的外层容器）：`AnchoredPanel` 的 Tab 处理判定
   * `anchor.contains(active)` 来决定"是不是从触发器上 Tab 出去、该不该跳进面板"，如果 anchor
   * 把输入框也包在里面，用户在输入框内打字后按 Tab 会被错误地拦进选项列表，而不是正常移动
   * 到下一个控件。触发器必须是不包含输入框的叶子元素，与 `Combobox.tsx` 的约定一致。
   */
  const activeAnchorRef = useRef<HTMLButtonElement | null>(null);

  function update(id: string, patch: Partial<KeywordRow>) {
    onChange(rows.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  }

  function remove(id: string) {
    const next = rows.filter((r) => r.id !== id);
    onChange(next.length > 0 ? next : [newKeywordRow()]);
    if (openRowId === id) setOpenRowId(null);
  }

  function openPanel(rowId: string) {
    setOpenRowId(rowId);
    setFilterText('');
    const input = inputRefs.current.get(rowId);
    input?.focus();
    input?.select();
  }

  function closePanel() {
    setOpenRowId(null);
  }

  function choosePreset(rowId: string, preset: string) {
    update(rowId, { target: preset });
    closePanel();
    inputRefs.current.get(rowId)?.focus();
  }

  return (
    <div className="mask-keyword-list">
      {rows.map((row, index) => {
        const isOpen = openRowId === row.id;
        if (isOpen) activeAnchorRef.current = triggerRefs.current.get(row.id) ?? null;
        const needle = filterText.trim().toLowerCase();
        const visiblePresets = TARGET_PRESETS.filter((p) => p.toLowerCase().includes(needle));

        return (
          <div key={row.id} className="mask-keyword-row">
            <input
              type="text"
              aria-label="Text to replace"
              placeholder="Text to replace"
              value={row.source}
              disabled={disabled}
              aria-invalid={invalid && index === 0 ? true : undefined}
              onChange={(e) => update(row.id, { source: e.target.value })}
            />
            <span className="mask-keyword-arrow" aria-hidden>
              →
            </span>
            <div className="mask-target-field">
              <input
                type="text"
                aria-label="Replace with"
                placeholder="Replace with"
                value={row.target}
                disabled={disabled}
                ref={(el) => {
                  if (el) inputRefs.current.set(row.id, el);
                  else inputRefs.current.delete(row.id);
                }}
                onChange={(e) => {
                  update(row.id, { target: e.target.value });
                  if (isOpen) setFilterText(e.target.value);
                }}
                onKeyDown={(e) => {
                  if (!isOpen && e.key === 'ArrowDown') {
                    e.preventDefault();
                    openPanel(row.id);
                  }
                }}
              />
              <button
                type="button"
                className="mask-target-trigger"
                aria-label="Choose replacement preset"
                aria-haspopup="listbox"
                aria-expanded={isOpen}
                disabled={disabled}
                ref={(el) => {
                  if (el) triggerRefs.current.set(row.id, el);
                  else triggerRefs.current.delete(row.id);
                }}
                onClick={() => (isOpen ? closePanel() : openPanel(row.id))}
              >
                <ChevronDownIcon size={14} aria-hidden />
              </button>
              {isOpen && (
                <AnchoredPanel
                  anchorRef={activeAnchorRef}
                  onClose={closePanel}
                  align="right"
                  minHeight={140}
                  maxHeight={220}
                  className="mask-target-panel"
                  role="listbox"
                  aria-label="Replacement presets"
                >
                  {visiblePresets.length === 0 ? (
                    <div className="mask-target-empty">No matches.</div>
                  ) : (
                    visiblePresets.map((preset) => (
                      <button
                        key={preset}
                        type="button"
                        role="option"
                        aria-selected={preset === row.target}
                        className={`mask-target-option${preset === row.target ? ' mask-target-option-active' : ''}`}
                        onClick={() => choosePreset(row.id, preset)}
                      >
                        {preset}
                      </button>
                    ))
                  )}
                </AnchoredPanel>
              )}
            </div>
            <button
              type="button"
              className="mask-keyword-remove"
              aria-label="Remove rule"
              disabled={disabled}
              onClick={() => remove(row.id)}
            >
              <CloseIcon size={14} />
            </button>
          </div>
        );
      })}
      <div className="mask-keyword-footer">
        <button
          type="button"
          className="mask-keyword-add"
          disabled={disabled || atLimit}
          onClick={() => onChange([...rows, newKeywordRow()])}
        >
          <PlusIcon size={14} /> Add rule
        </button>
        <span className={`mask-keyword-count${atLimit ? ' at-limit' : ''}`}>
          {atLimit ? `${rows.length} / ${maxRows} — limit reached` : `${rows.length} / ${maxRows}`}
        </span>
      </div>
    </div>
  );
}
