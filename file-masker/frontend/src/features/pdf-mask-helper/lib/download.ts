/** `a.pdf` → `a_masked.pdf`；无扩展名 → `<name>_masked.pdf`（语义移植自老 pdf_mask.html）。 */
export function buildMaskedName(fileName: string): string {
  if (!fileName) return 'masked.pdf';
  const dot = fileName.lastIndexOf('.');
  if (dot > 0) {
    return `${fileName.slice(0, dot)}_masked${fileName.slice(dot)}`;
  }
  return `${fileName}_masked.pdf`;
}

/** 触发浏览器保存（anchor click；URL 延迟 revoke，给下载留时间）。 */
export function saveBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}
