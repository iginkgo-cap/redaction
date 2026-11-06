import { useState } from 'react';
import { CloseIcon, PlusIcon } from '../../components/icons';

/**
 * In-memory File[] source list for PDF Masking (LOOK-5885). Restored from the retired
 * CtcPdfHelper FileList (the ctc-sessions FileList is fileId/session-document based and
 * doesn't fit — masking never persists sources as session documents). Reuses the shared
 * `file-list*` styles.
 */
interface FileListProps {
  files: File[];
  selectedIndex: number;
  onSelect: (index: number) => void;
  onRemove: (index: number) => void;
  onAddMore: () => void;
  onFilesDropped: (files: FileList) => void;
  /** 点了主操作但一个文件都没加：给列表打上红框。 */
  invalid?: boolean;
}

export default function FileList({ files, selectedIndex, onSelect, onRemove, onAddMore, onFilesDropped, invalid }: FileListProps) {
  const [dragging, setDragging] = useState(false);

  function handleDragOver(e: React.DragEvent) {
    e.preventDefault();
    setDragging(true);
  }

  function handleDragLeave(e: React.DragEvent) {
    if (e.currentTarget.contains(e.relatedTarget as Node)) return;
    setDragging(false);
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragging(false);
    if (e.dataTransfer.files.length > 0) {
      onFilesDropped(e.dataTransfer.files);
    }
  }

  return (
    <div
      className={`file-list${dragging ? ' drag-over' : ''}${invalid ? ' invalid' : ''}`}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {files.map((file, index) => (
        <div
          key={`${file.name}-${file.size}-${file.lastModified}`}
          className={`file-list-item ${index === selectedIndex ? 'selected' : ''}`}
        >
          {/* A real button, not a clickable row: selecting drives the preview, and with 2+ files
              a div would leave a keyboard user unable to switch which one is shown.
              `.file-list-item-info` already neutralizes the UA button chrome (LOOK-5932). */}
          <button
            type="button"
            className="file-list-item-info"
            title={`Preview ${file.name}`}
            aria-current={index === selectedIndex || undefined}
            onClick={() => onSelect(index)}
          >
            <span className="file-list-item-name">{file.name}</span>
            <span className="file-list-item-size">{formatFileSize(file.size)}</span>
          </button>
          <div className="file-list-item-actions">
            <button
              type="button"
              className="file-list-remove-btn"
              title="Remove file"
              onClick={(e) => { e.stopPropagation(); onRemove(index); }}
            >
              <CloseIcon size={14} />
            </button>
          </div>
        </div>
      ))}

      <button type="button" className="file-list-add-btn" onClick={onAddMore}>
        <PlusIcon size={14} />
        {files.length === 0 ? 'Drop files here or click to select' : 'Drop or select more files'}
      </button>
    </div>
  );
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}
