import { describe, it, expect, afterEach } from 'vitest';
import { useState } from 'react';
import { byLabel, click, flush, renderComponent, type RenderResult } from '../../../testing/renderComponent';
import KeywordRows, { newKeywordRow, type KeywordRow } from '../KeywordRows';

/**
 * The preset panel is portaled to `document.body` by `AnchoredPanel` (LOOK-5986), so anything
 * inside it is queried from `document`, not from the render result's own container.
 */
function panelOptions(): HTMLButtonElement[] {
  return [...document.querySelectorAll<HTMLButtonElement>('.mask-target-panel [role="option"]')];
}

function panel(): HTMLElement | null {
  return document.querySelector<HTMLElement>('.mask-target-panel');
}

async function pressKey(on: Element, key: string) {
  await flush();
  on.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }));
  await flush();
}

/** Stateful host so `onChange` round-trips like the real `PdfMaskHelper` parent. */
function Harness({ initialRows, disabled }: { initialRows: KeywordRow[]; disabled?: boolean }) {
  const [rows, setRows] = useState(initialRows);
  return <KeywordRows rows={rows} onChange={setRows} disabled={disabled} maxRows={100} />;
}

describe('KeywordRows — default replacement + preset picker (LOOK-6613)', () => {
  let result: RenderResult | null = null;

  afterEach(() => {
    result?.unmount();
    result = null;
  });

  it('newKeywordRow() defaults target to [REDACTED]', () => {
    expect(newKeywordRow().target).toBe('[REDACTED]');
  });

  it('renders the default value in a freshly added row', async () => {
    result = await renderComponent(<Harness initialRows={[newKeywordRow()]} />);
    const targetInput = byLabel<HTMLInputElement>(result, 'Replace with');
    expect(targetInput?.value).toBe('[REDACTED]');
  });

  it('shows all 3 presets when the field still holds the default value (v2 fix)', async () => {
    result = await renderComponent(<Harness initialRows={[newKeywordRow()]} />);
    const trigger = byLabel<HTMLButtonElement>(result, 'Choose replacement preset');
    await click(trigger);

    const labels = panelOptions().map((b) => (b.textContent ?? '').trim());
    expect(labels).toEqual(['[REDACTED]', '[REDACTED_PERSON_NAME]', '[REDACTED_COMPANY_NAME]']);
  });

  it('picking a preset writes it to target and closes the panel', async () => {
    result = await renderComponent(<Harness initialRows={[newKeywordRow()]} />);
    const trigger = byLabel<HTMLButtonElement>(result, 'Choose replacement preset');
    await click(trigger);

    const personOption = panelOptions().find((b) => b.textContent?.includes('PERSON_NAME'));
    await click(personOption ?? null);

    const targetInput = byLabel<HTMLInputElement>(result, 'Replace with');
    expect(targetInput?.value).toBe('[REDACTED_PERSON_NAME]');
    expect(panel()).toBeNull();
  });

  it('free typing overrides the default value', async () => {
    result = await renderComponent(<Harness initialRows={[newKeywordRow()]} />);
    const targetInput = byLabel<HTMLInputElement>(result, 'Replace with')!;
    const proto = HTMLInputElement.prototype;
    const nativeSetter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    nativeSetter?.call(targetInput, '***');
    targetInput.dispatchEvent(new Event('input', { bubbles: true }));
    await flush();

    expect(byLabel<HTMLInputElement>(result, 'Replace with')?.value).toBe('***');
  });

  it('clearing the field keeps the empty-string = delete semantics', async () => {
    result = await renderComponent(<Harness initialRows={[newKeywordRow()]} />);
    const targetInput = byLabel<HTMLInputElement>(result, 'Replace with')!;
    const proto = HTMLInputElement.prototype;
    const nativeSetter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    nativeSetter?.call(targetInput, '');
    targetInput.dispatchEvent(new Event('input', { bubbles: true }));
    await flush();

    expect(byLabel<HTMLInputElement>(result, 'Replace with')?.value).toBe('');
  });

  it('typing after opening filters the list (substring, case-insensitive)', async () => {
    result = await renderComponent(<Harness initialRows={[newKeywordRow()]} />);
    const trigger = byLabel<HTMLButtonElement>(result, 'Choose replacement preset');
    await click(trigger);

    const targetInput = byLabel<HTMLInputElement>(result, 'Replace with')!;
    const proto = HTMLInputElement.prototype;
    const nativeSetter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    nativeSetter?.call(targetInput, 'person');
    targetInput.dispatchEvent(new Event('input', { bubbles: true }));
    await flush();

    const labels = panelOptions().map((b) => (b.textContent ?? '').trim());
    expect(labels).toEqual(['[REDACTED_PERSON_NAME]']);
  });

  it('no matches shows the empty state without correcting the typed text', async () => {
    result = await renderComponent(<Harness initialRows={[newKeywordRow()]} />);
    const trigger = byLabel<HTMLButtonElement>(result, 'Choose replacement preset');
    await click(trigger);

    const targetInput = byLabel<HTMLInputElement>(result, 'Replace with')!;
    const proto = HTMLInputElement.prototype;
    const nativeSetter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    nativeSetter?.call(targetInput, '***');
    targetInput.dispatchEvent(new Event('input', { bubbles: true }));
    await flush();

    expect(panelOptions()).toHaveLength(0);
    expect(panel()?.textContent).toContain('No matches.');
    expect(byLabel<HTMLInputElement>(result, 'Replace with')?.value).toBe('***');
  });

  it('reopening resets the filter to show all 3 presets again (v2 fix)', async () => {
    result = await renderComponent(<Harness initialRows={[newKeywordRow()]} />);
    const trigger = byLabel<HTMLButtonElement>(result, 'Choose replacement preset')!;
    await click(trigger);

    const targetInput = byLabel<HTMLInputElement>(result, 'Replace with')!;
    const proto = HTMLInputElement.prototype;
    const nativeSetter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    nativeSetter?.call(targetInput, 'person');
    targetInput.dispatchEvent(new Event('input', { bubbles: true }));
    await flush();
    expect(panelOptions()).toHaveLength(1);

    // Close (Escape) then reopen — the stale filter must not carry over.
    await pressKey(targetInput, 'Escape');
    expect(panel()).toBeNull();

    await click(byLabel<HTMLButtonElement>(result, 'Choose replacement preset'));
    const labels = panelOptions().map((b) => (b.textContent ?? '').trim());
    expect(labels).toEqual(['[REDACTED]', '[REDACTED_PERSON_NAME]', '[REDACTED_COMPANY_NAME]']);
  });

  it('opening the panel selects the existing text in the input', async () => {
    result = await renderComponent(<Harness initialRows={[newKeywordRow()]} />);
    const trigger = byLabel<HTMLButtonElement>(result, 'Choose replacement preset');
    await click(trigger);

    const targetInput = byLabel<HTMLInputElement>(result, 'Replace with')!;
    expect(targetInput.selectionStart).toBe(0);
    expect(targetInput.selectionEnd).toBe(targetInput.value.length);
  });

  it('Tab from the input is not hijacked into the panel (anchor must be the trigger, not the field)', async () => {
    // AnchoredPanel treats Tab off anything *inside* its anchor as "leaving the trigger" and steps
    // focus into the panel instead (see AnchoredPanel.tsx's Tab handler). If the anchor were the
    // `.mask-target-field` wrapper (which also contains the text input), typing in the input and
    // pressing Tab would get redirected into the option list instead of the next control.
    result = await renderComponent(<Harness initialRows={[newKeywordRow()]} />);
    const trigger = byLabel<HTMLButtonElement>(result, 'Choose replacement preset');
    await click(trigger);
    const targetInput = byLabel<HTMLInputElement>(result, 'Replace with')!;
    expect(document.activeElement).toBe(targetInput);

    const event = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true });
    let notPrevented = true;
    await flush();
    notPrevented = targetInput.dispatchEvent(event);
    await flush();

    expect(notPrevented).toBe(true);
    expect(panelOptions().some((o) => o === document.activeElement)).toBe(false);
  });

  it('Escape closes the panel without changing the committed text', async () => {
    result = await renderComponent(<Harness initialRows={[newKeywordRow()]} />);
    const trigger = byLabel<HTMLButtonElement>(result, 'Choose replacement preset');
    await click(trigger);
    const targetInput = byLabel<HTMLInputElement>(result, 'Replace with')!;

    await pressKey(targetInput, 'Escape');

    expect(panel()).toBeNull();
    expect(byLabel<HTMLInputElement>(result, 'Replace with')?.value).toBe('[REDACTED]');
  });

  it('disabled rows cannot open the preset panel', async () => {
    result = await renderComponent(<Harness initialRows={[newKeywordRow()]} disabled />);
    const trigger = byLabel<HTMLButtonElement>(result, 'Choose replacement preset');
    expect(trigger?.disabled).toBe(true);
    await click(trigger);
    expect(panel()).toBeNull();
  });
});
