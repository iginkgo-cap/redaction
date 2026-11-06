import { useEffect, useMemo, useRef, useState } from 'react';
import Spinner from '../../components/Spinner';
import { fileToUint8Array, PdfCanvas } from '../../lib/pdf';
import { useZoomPan } from '../../lib/zoom/useZoomPan';
import ZoomControls from '../../lib/zoom/ZoomControls';
import FileList from './FileList';
import KeywordRows, { newKeywordRow, type KeywordRow } from './KeywordRows';
import { maskPdf } from './lib/api';
import { buildMaskedName, saveBlob } from './lib/download';
import {
  MaskResultCache,
  fileKey,
  keywordsCacheKey,
  loadSavedKeywords,
  normalizeKeywords,
  saveKeywords,
} from './lib/maskingSession';

const PREVIEW_PAGE_WIDTH = 1240; // 与 CTC 预览一致
const PREVIEW_DEBOUNCE_MS = 1500;
/** 沿用合规工具的上限（LOOK-5885）。 */
const MAX_FILES = 20;
/** 与 file-masker 服务端的关键词上限（100 条）一致。 */
const MAX_KEYWORD_ROWS = 100;

/** 与 file-masker `MAX_FILE_MB` 默认值一致——先在前端拦掉，省一次白跑的上传。 */
const MAX_FILE_BYTES = 100 * 1024 * 1024;

/**
 * PDF Masking workspace —— 移植自合规工具（LOOK-5885），file-masker 离线版（LOOK-5163）。
 * 左栏：文件列表 + 打码规则行；右栏：实时预览（无规则 = 本地渲染原文；有规则 =
 * debounce 后调用本地 /mask，stale-while-revalidate）。文件直接上传给本地服务，
 * 不经过 S3；关键词保存在本浏览器的 localStorage。
 */
export default function PdfMaskHelper() {
  const [sourceFiles, setSourceFiles] = useState<File[]>([]);
  const [selectedFileIndex, setSelectedFileIndex] = useState(0);
  const [keywordRows, setKeywordRows] = useState<KeywordRow[]>(() => {
    const saved = loadSavedKeywords();
    return saved.length > 0 ? saved.map((k) => ({ ...newKeywordRow(), ...k })) : [newKeywordRow()];
  });
  const [previewBytes, setPreviewBytes] = useState<Uint8Array | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewStale, setPreviewStale] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  // 与 errorMessage 分开：预览成功会清掉 errorMessage，而「有文件没被加进来」这种告知
  // 必须留在屏上，直到用户下一次选文件。
  const [fileNotice, setFileNotice] = useState<string | null>(null);
  // 主操作不按有效性置灰（LOOK-5546）：点了才校验，把原因落在出错的那个字段上。
  const [filesError, setFilesError] = useState<string | null>(null);
  const [rulesError, setRulesError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [downloadProgress, setDownloadProgress] = useState<{ current: number; total: number } | null>(null);
  const zoom = useZoomPan();
  const sourceFileInputRef = useRef<HTMLInputElement>(null);

  const cacheRef = useRef(new MaskResultCache());
  const generationRef = useRef(0);

  const selectedFile = sourceFiles[selectedFileIndex] ?? null;
  const normalizedKeywords = useMemo(
    () => normalizeKeywords(keywordRows.map(({ source, target }) => ({ source, target }))),
    [keywordRows],
  );
  const kwKey = useMemo(() => keywordsCacheKey(normalizedKeywords), [normalizedKeywords]);

  /** 规则行变了就清掉它的报错——校验错误一改就消失，别赖在屏上。 */
  function handleKeywordRowsChange(next: KeywordRow[]) {
    setKeywordRows(next);
    saveKeywords(next.map(({ source, target }) => ({ source, target })));
    setRulesError(null);
  }

  function handleFilesSelected(fileList: FileList | null) {
    if (!fileList || fileList.length === 0) return;
    const pdfs = Array.from(fileList).filter(
      (f) => f.type === 'application/pdf' || f.name.toLowerCase().endsWith('.pdf'),
    );
    if (pdfs.length === 0) {
      setFileNotice('Only PDF files are supported.');
      return;
    }
    // 全部在更新函数外算好：更新函数必须是纯的（StrictMode 会跑两次），而且它的执行时机
    // 晚于下面的 setErrorMessage，在里面收集提示会读到空数组。
    const oversized = pdfs.filter((f) => f.size > MAX_FILE_BYTES);
    const deduped = pdfs.filter(
      (f) =>
        f.size <= MAX_FILE_BYTES &&
        !sourceFiles.some(
          (p) => p.name === f.name && p.size === f.size && p.lastModified === f.lastModified,
        ),
    );
    const accepted = deduped.slice(0, Math.max(MAX_FILES - sourceFiles.length, 0));

    const rejections: string[] = [];
    if (oversized.length > 0) {
      rejections.push(
        `Too large (max ${MAX_FILE_BYTES / (1024 * 1024)} MB): ${oversized.map((f) => f.name).join(', ')}`,
      );
    }
    if (deduped.length > accepted.length) {
      rejections.push(`At most ${MAX_FILES} files at a time — ${deduped.length - accepted.length} not added.`);
    }

    if (accepted.length > 0) {
      setSourceFiles((prev) => [...prev, ...accepted]);
      if (sourceFiles.length === 0) setSelectedFileIndex(0);
      setFilesError(null);
    }
    setFileNotice(rejections.length > 0 ? rejections.join(' ') : null);
  }

  /** 释放某个文件占的缓存 —— 不做的话一次会话里反复加删文件，内存只增不减。 */
  function releaseCachesFor(file: File) {
    cacheRef.current.dropByFile(fileKey(file));
  }

  function handleRemoveFile(index: number) {
    const removed = sourceFiles[index];
    if (removed) releaseCachesFor(removed);
    setSourceFiles((prev) => {
      const next = prev.filter((_, i) => i !== index);
      setSelectedFileIndex((sel) => {
        if (next.length === 0) return 0;
        if (sel >= next.length) return next.length - 1;
        if (index < sel) return sel - 1;
        return sel;
      });
      return next;
    });
  }

  /** 拿某文件在当前关键词下的打码字节：缓存命中直接返回。 */
  async function getMaskedBytes(file: File, keywordsKey: string): Promise<Uint8Array> {
    const id = fileKey(file);
    const cached = cacheRef.current.get(id, keywordsKey);
    if (cached) return cached;
    const bytes = await maskPdf(file, normalizedKeywords);
    cacheRef.current.set(id, keywordsKey, bytes);
    return bytes;
  }

  // 预览：无有效关键词 → 本地渲染原文（零服务端调用）；有 → debounce 后调用本地 /mask，
  // 旧预览留屏 + Updating… chip（stale-while-revalidate）。
  useEffect(() => {
    if (!selectedFile) {
      setPreviewBytes(null);
      setPreviewLoading(false);
      setPreviewStale(false);
      return;
    }

    let cancelled = false;
    const generation = ++generationRef.current;

    if (normalizedKeywords.length === 0) {
      setPreviewBytes(null);
      setPreviewLoading(true);
      setPreviewStale(false);
      void (async () => {
        try {
          const bytes = await fileToUint8Array(selectedFile);
          if (cancelled || generationRef.current !== generation) return;
          setPreviewBytes(bytes);
          setErrorMessage(null);
        } catch (error) {
          if (cancelled || generationRef.current !== generation) return;
          setErrorMessage(error instanceof Error ? error.message : 'Failed to render preview.');
        } finally {
          if (!cancelled && generationRef.current === generation) setPreviewLoading(false);
        }
      })();
      return () => {
        cancelled = true;
      };
    }

    setPreviewStale(true);
    const timer = setTimeout(async () => {
      if (cancelled) return;
      try {
        const bytes = await getMaskedBytes(selectedFile, kwKey);
        if (cancelled || generationRef.current !== generation) return;
        setPreviewBytes(bytes);
        setErrorMessage(null);
      } catch (error) {
        if (cancelled || generationRef.current !== generation) return;
        setErrorMessage(error instanceof Error ? error.message : 'Masking service unavailable.');
      } finally {
        if (!cancelled && generationRef.current === generation) {
          setPreviewLoading(false);
          setPreviewStale(false);
        }
      }
    }, PREVIEW_DEBOUNCE_MS);

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // normalizedKeywords 由 kwKey 完全决定，不重复列入
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedFile, kwKey]);

  async function handleDownloadAll() {
    // 按钮只被 downloading 挡住；缺文件/缺规则在这里校验，并把原因写到对应字段上。
    const noFiles = sourceFiles.length === 0;
    const noRules = normalizedKeywords.length === 0;
    setFilesError(noFiles ? 'Add at least one PDF.' : null);
    setRulesError(noRules ? 'Add at least one masking rule.' : null);
    if (noFiles || noRules) return;

    setDownloading(true);
    setDownloadProgress({ current: 0, total: sourceFiles.length });
    const failures: string[] = [];
    try {
      for (let i = 0; i < sourceFiles.length; i++) {
        const file = sourceFiles[i];
        setDownloadProgress({ current: i + 1, total: sourceFiles.length });
        try {
          const bytes = await getMaskedBytes(file, kwKey);
          saveBlob(new Blob([bytes as BlobPart], { type: 'application/pdf' }), buildMaskedName(file.name));
        } catch (err) {
          failures.push(`${file.name} (${err instanceof Error ? err.message : 'Masking failed.'})`);
        }
      }
      setErrorMessage(failures.length > 0 ? `Failed to mask: ${failures.join(', ')}` : null);
    } finally {
      setDownloading(false);
      setDownloadProgress(null);
    }
  }

  return (
    <div className="ctc-page">
      <div className="ctc-toolbar" role="toolbar" aria-label="PDF masking actions">
        <div className="mask-toolbar-spacer" />
        <button
          type="button"
          className="pdf-forms-cta"
          onClick={handleDownloadAll}
          disabled={downloading}
        >
          {downloading ? (
            <>
              <Spinner /> Masking…
              {downloadProgress ? ` (${downloadProgress.current}/${downloadProgress.total})` : ''}
            </>
          ) : sourceFiles.length > 1 ? (
            // QuickForm 的 `Generate N PDFs` 句式，不用 `… All (N)`
            `Mask & Download ${sourceFiles.length} PDFs`
          ) : (
            'Mask & Download'
          )}
        </button>
      </div>

      <section className="workspace">
        <div className="left-column">
          <div className="panel form-panel">
            <div className="field">
              <div className="field-header">
                <span>
                  Source files <span className="pdf-forms-field-required">*</span>
                </span>
                {sourceFiles.length > 0 && (
                  <button
                    type="button"
                    className="clear-all-btn"
                    onClick={() => {
                      setSourceFiles([]);
                      setSelectedFileIndex(0);
                      setPreviewBytes(null);
                      setErrorMessage(null);
                      setFileNotice(null);
                      cacheRef.current.clear();
                    }}
                  >
                    Clear All
                  </button>
                )}
              </div>
              <input
                ref={sourceFileInputRef}
                type="file"
                multiple
                accept="application/pdf"
                style={{ display: 'none' }}
                onChange={(e) => {
                  handleFilesSelected(e.target.files);
                  e.target.value = '';
                }}
              />
              <FileList
                files={sourceFiles}
                selectedIndex={selectedFileIndex}
                onSelect={setSelectedFileIndex}
                onRemove={handleRemoveFile}
                onAddMore={() => sourceFileInputRef.current?.click()}
                onFilesDropped={handleFilesSelected}
                invalid={!!filesError}
              />
              {filesError ? (
                <p className="pdf-forms-field-error" role="alert">
                  {filesError}
                </p>
              ) : null}
            </div>

            <div className="field">
              <div className="field-header">
                <span>
                  Masking rules <span className="pdf-forms-field-required">*</span>
                </span>
              </div>
              <KeywordRows
                rows={keywordRows}
                onChange={handleKeywordRowsChange}
                maxRows={MAX_KEYWORD_ROWS}
                invalid={!!rulesError}
              />
              {rulesError ? (
                <p className="pdf-forms-field-error" role="alert">
                  {rulesError}
                </p>
              ) : null}
              {/* 「留空 = 删除」原来塞在占位符里，420px 面板放不下被截成
                  "Replace with (empty"。占位符只留字段名，行为说明放这儿。 */}
              <p className="mask-rules-hint">
                Matches are case-insensitive. Leave “Replace with” empty to remove the text.
              </p>
              <p className="mask-preview-hint">
                Masking can miss text. Check each PDF before sharing.
              </p>
            </div>

            {fileNotice ? <p className="status error" role="alert">{fileNotice}</p> : null}
            {errorMessage ? <p className="status error" role="alert">{errorMessage}</p> : null}
          </div>
        </div>

        <div className="panel preview-panel">
          {selectedFile?.name && <div className="preview-file-header">{selectedFile.name}</div>}
          <div className="zoom-viewport">
            <ZoomControls zoom={zoom} disabled={previewLoading || previewBytes === null} />
            {previewLoading && previewBytes === null ? (
              <div className="preview-placeholder">
                <div className="spinner" />
                <p>Generating preview…</p>
              </div>
            ) : previewBytes !== null ? (
              <>
                <div className="zoom-scroll" ref={zoom.containerRef}>
                  <div className="zoom-sizer" style={zoom.sizerStyle}>
                    <div className="zoom-content" ref={zoom.contentRef} style={zoom.contentStyle}>
                      <PdfCanvas
                        pdfBytes={previewBytes}
                        pageWidthPx={PREVIEW_PAGE_WIDTH}
                        zoomScale={zoom.settledScale}
                      />
                    </div>
                  </div>
                </div>
                {/* CTC 的刷新态：半透明蒙版 + 居中 spinner，盖在旧预览上。
                    旧预览仍然透出来，stale-while-revalidate 的语义不变。 */}
                {previewStale && (
                  <div className="preview-refresh-overlay" role="status" aria-label="Refreshing preview">
                    <div className="spinner" />
                  </div>
                )}
              </>
            ) : (
              <div className="preview-placeholder">
                <p>No preview yet.</p>
                <span>Add PDF files to see preview.</span>
              </div>
            )}
          </div>
        </div>
      </section>
    </div>
  );
}
