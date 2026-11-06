import { useEffect, useState } from 'react';
import MailPage from './features/mail-demo/MailPage';
import PdfMaskHelper from './features/pdf-mask-helper/PdfMaskHelper';

/** /mail 是邮件打码页（LOOK-6764），其余路径是 PDF 打码页。由 FastAPI 托管同一个 index.html。 */
function isMailPage(): boolean {
  return window.location.pathname.replace(/\/+$/, '').endsWith('/mail');
}

export default function App() {
  const mailPage = isMailPage();
  const [version, setVersion] = useState('');
  useEffect(() => {
    if (mailPage) document.title = 'Mail Masking';
  }, [mailPage]);
  useEffect(() => {
    fetch('health')
      .then((r) => r.json())
      .then((d: { version?: string }) => setVersion(d.version ?? ''))
      .catch(() => setVersion(''));
  }, []);
  // 外壳沿用合规工具的 .app-shell / .app-header（Workspace.tsx），面包屑只是文本、不跳转
  return (
    <main className="app-shell mask-shell">
      <header className="app-header">
        <div className="app-header-title">
          <span className="app-header-title-app">Ginkgo Compliance Tools</span>
          <span className="app-header-title-sep" aria-hidden="true">
            ›
          </span>
          <span>{mailPage ? 'Mail Masking' : 'PDF Masking'}</span>
        </div>
      </header>
      {mailPage ? <MailPage /> : <PdfMaskHelper />}
      <footer className="mask-version">{version ? `file-masker ${version}` : null}</footer>
    </main>
  );
}
