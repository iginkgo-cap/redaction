import { beforeEach, describe, expect, it } from 'vitest';
import {
  MaskResultCache, fileKey, keywordsCacheKey, loadSavedKeywords, normalizeKeywords, saveKeywords,
} from '../maskingSession';

describe('MaskResultCache keyed by file identity', () => {
  const a = new File([new Uint8Array([1])], 'a.pdf', { lastModified: 1 });
  const b = new File([new Uint8Array([1, 2])], 'b.pdf', { lastModified: 2 });

  it('caches per file and keyword set, evicting the oldest entry', () => {
    const cache = new MaskResultCache(2);
    cache.set(fileKey(a), 'k1', new Uint8Array([1]));
    cache.set(fileKey(b), 'k1', new Uint8Array([2]));
    cache.get(fileKey(a), 'k1');                       // 刷新 a
    cache.set(fileKey(a), 'k2', new Uint8Array([3]));  // 淘汰 b|k1
    expect(cache.get(fileKey(b), 'k1')).toBeUndefined();
    expect(cache.get(fileKey(a), 'k1')).toEqual(new Uint8Array([1]));
  });

  it('dropByFile removes every keyword variant of that file', () => {
    const cache = new MaskResultCache();
    cache.set(fileKey(a), 'k1', new Uint8Array([1]));
    cache.set(fileKey(a), 'k2', new Uint8Array([2]));
    cache.dropByFile(fileKey(a));
    expect(cache.get(fileKey(a), 'k1')).toBeUndefined();
    expect(cache.get(fileKey(a), 'k2')).toBeUndefined();
  });
});

describe('keyword persistence', () => {
  beforeEach(() => localStorage.clear());

  it('round-trips rows through localStorage', () => {
    saveKeywords([{ source: 'Zhang San', target: '***' }]);
    expect(loadSavedKeywords()).toEqual([{ source: 'Zhang San', target: '***' }]);
  });

  it('returns [] when storage is empty or corrupted', () => {
    expect(loadSavedKeywords()).toEqual([]);
    localStorage.setItem('file-masker.keywords', '{not json');
    expect(loadSavedKeywords()).toEqual([]);
  });
});

describe('normalizeKeywords (unchanged from compliance-tools)', () => {
  it('trims, drops empty rows and lets later duplicates win', () => {
    expect(normalizeKeywords([{ source: ' a b ', target: '1' }, { source: '', target: 'x' }, { source: 'a b', target: '2' }]))
      .toEqual([{ source: 'a b', target: '2' }]);
    expect(keywordsCacheKey([{ source: 'a', target: '' }])).toBe('[["a",""]]');
  });
});
