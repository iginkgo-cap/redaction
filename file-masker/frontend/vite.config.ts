import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { viteStaticCopy } from 'vite-plugin-static-copy';

export default defineConfig({
  // 相对路径：页面由 FastAPI 在 / 下托管，资源在 /assets
  base: './',
  plugins: [
    react(),
    // pdf.js 的 cMap 与标准字体打包到本地，离线预览中文 PDF 不缺字（与合规工具的有意差异）
    viteStaticCopy({
      targets: [
        { src: 'node_modules/pdfjs-dist/cmaps/*', dest: 'cmaps' },
        { src: 'node_modules/pdfjs-dist/standard_fonts/*', dest: 'standard_fonts' },
      ],
    }),
  ],
  build: { outDir: '../file_masker/web/static', emptyOutDir: true },
  server: { proxy: { '/mask': 'http://127.0.0.1:8080', '/health': 'http://127.0.0.1:8080', '/api': 'http://127.0.0.1:8080' } },
  test: { environment: 'happy-dom' },
});
