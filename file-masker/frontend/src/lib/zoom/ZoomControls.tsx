import type { ZoomApi } from './useZoomPan';

interface Props {
  zoom: ZoomApi;
  disabled?: boolean;
}

export default function ZoomControls({ zoom, disabled = false }: Props) {
  return (
    <div className="zoom-controls" role="toolbar" aria-label="Zoom controls">
      <button
        type="button"
        className="zoom-btn"
        onClick={zoom.zoomOut}
        disabled={disabled}
        aria-label="Zoom out"
        title="Zoom out"
      >
        −
      </button>
      <span className="zoom-percent" aria-live="polite">
        {zoom.percent}%
      </span>
      <button
        type="button"
        className="zoom-btn"
        onClick={zoom.zoomIn}
        disabled={disabled}
        aria-label="Zoom in"
        title="Zoom in"
      >
        +
      </button>
      <button
        type="button"
        className="zoom-btn zoom-btn-text"
        onClick={zoom.fitWidth}
        disabled={disabled}
        title="Fit width"
      >
        Fit
      </button>
      <button
        type="button"
        className="zoom-btn zoom-btn-text"
        onClick={zoom.actualSize}
        disabled={disabled}
        title="Actual size (100%)"
      >
        100%
      </button>
    </div>
  );
}
