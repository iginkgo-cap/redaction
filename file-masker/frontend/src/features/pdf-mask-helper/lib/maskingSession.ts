/**
 * PDF Masking 的 framework-free 核心（LOOK-5885）：
 * 关键词规范化 / 缓存键、打码结果 LRU 缓存、关键词本地持久化（file-masker：无 S3 上传）。
 * 全部纯数据结构，不碰 React / fetch。
 */

export interface MaskKeyword {
  source: string;
  target: string;
}

/** trim source、丢空行；同 source 后者覆盖前者（对齐后端 python dict 语义）；target 缺省 → ''。 */
export function normalizeKeywords(rows: MaskKeyword[]): MaskKeyword[] {
  const bySource = new Map<string, string>();
  for (const row of rows) {
    const source = (row.source ?? '').trim();
    if (!source) continue;
    bySource.set(source, row.target ?? '');
  }
  return [...bySource.entries()].map(([source, target]) => ({ source, target }));
}

/** 规范化结果的稳定序列化，作为 MaskResultCache 的关键词维度键。 */
export function keywordsCacheKey(normalized: MaskKeyword[]): string {
  return JSON.stringify(normalized.map((k) => [k.source, k.target]));
}

/** File 的身份键（与 CTC 去重语义一致：name+size+lastModified）。 */
export function fileKey(file: File): string {
  return `${file.name}|${file.size}|${file.lastModified}`;
}

/**
 * 打码结果 LRU 缓存：键 `${fileId}|${kwKey}`（fileId = fileKey(file)），值为打码后 PDF 字节。
 * 切文件 / 撤销输入 / 重输旧值时避免重复的后端往返。
 */
export class MaskResultCache {
  // Map 迭代序 = 插入序，作 LRU 用
  private readonly entries = new Map<string, Uint8Array>();

  constructor(private readonly maxEntries = 10) {}

  private key(fileId: string, kwKey: string): string {
    return `${fileId}|${kwKey}`;
  }

  get(fileId: string, kwKey: string): Uint8Array | undefined {
    const key = this.key(fileId, kwKey);
    const value = this.entries.get(key);
    if (value !== undefined) {
      // 刷新 recency：删除后重插到末尾
      this.entries.delete(key);
      this.entries.set(key, value);
    }
    return value;
  }

  set(fileId: string, kwKey: string, bytes: Uint8Array): void {
    const key = this.key(fileId, kwKey);
    this.entries.delete(key);
    this.entries.set(key, bytes);
    while (this.entries.size > this.maxEntries) {
      const oldest = this.entries.keys().next().value as string;
      this.entries.delete(oldest);
    }
  }

  dropByFile(fileId: string): void {
    for (const key of [...this.entries.keys()]) {
      if (key.startsWith(`${fileId}|`)) {
        this.entries.delete(key);
      }
    }
  }

  clear(): void {
    this.entries.clear();
  }
}

const STORAGE_KEY = 'file-masker.keywords';

/** 读取上次保存的关键词行；无痕模式、存储被禁用或数据损坏时返回 []。 */
export function loadSavedKeywords(): MaskKeyword[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '[]');
    return Array.isArray(parsed)
      ? parsed.map((k) => ({ source: String(k?.source ?? ''), target: String(k?.target ?? '') }))
      : [];
  } catch {
    return [];
  }
}

/** 保存关键词行；存储不可用时静默放弃（页面照常可用，只是不记忆）。 */
export function saveKeywords(rows: MaskKeyword[]): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(rows));
  } catch {
    /* 不记忆 */
  }
}
