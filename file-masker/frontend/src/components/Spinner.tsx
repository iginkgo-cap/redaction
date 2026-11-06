import type { CSSProperties } from 'react';

interface Props {
  /** CSS size; defaults to '0.9em' so it scales with the surrounding font
   *  without extending the line-box (text glyphs span roughly the same
   *  vertical area at 0.9em, so the parent's height doesn't change). */
  size?: string | number;
  className?: string;
  style?: CSSProperties;
  /** Accessible label for screen readers; defaults to 'Loading'. */
  label?: string;
}

/**
 * Tiny inline SVG spinner used inside buttons (and small status spans)
 * whenever the app is doing work that the user kicked off.
 *
 * Project rule: every button that performs async work shows this spinner
 * in its text area while busy. Render the spinner inline followed by a
 * short verb-phrase label:
 *
 *   <button disabled={saving}>
 *     {saving ? <><Spinner /> Saving…</> : 'Save'}
 *   </button>
 *
 * The spinner uses inline-block + vertical-align:middle + a small
 * right-margin, so it slips into the existing line-box without changing
 * the button's height.
 */
export default function Spinner({ size = '0.9em', className, style, label = 'Loading' }: Props) {
  return (
    <span
      role="status"
      aria-label={label}
      className={'spinner-inline' + (className ? ' ' + className : '')}
      style={{ width: size, height: size, ...style }}
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.5} aria-hidden="true">
        <circle cx="12" cy="12" r="9" opacity="0.25" />
        <path d="M21 12a9 9 0 0 0-9-9" strokeLinecap="round" />
      </svg>
    </span>
  );
}
