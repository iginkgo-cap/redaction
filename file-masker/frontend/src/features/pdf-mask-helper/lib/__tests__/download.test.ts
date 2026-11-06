import { describe, expect, it } from 'vitest';
import { buildMaskedName } from '../download';

describe('buildMaskedName', () => {
  it('inserts _masked before the extension', () => {
    expect(buildMaskedName('report.pdf')).toBe('report_masked.pdf');
  });

  it('handles multi-dot names', () => {
    expect(buildMaskedName('2026.06.statement.pdf')).toBe('2026.06.statement_masked.pdf');
  });

  it('appends _masked.pdf when no extension', () => {
    expect(buildMaskedName('statement')).toBe('statement_masked.pdf');
  });
});
