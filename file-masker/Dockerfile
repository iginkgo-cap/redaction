FROM node:22-alpine AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend ./
RUN npm run build   # 输出到 /src/file_masker/web/static

FROM python:3.10-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml LICENSE README.md ./
COPY file_masker ./file_masker
COPY --from=frontend /src/file_masker/web/static ./file_masker/web/static
RUN pip install --no-cache-dir ".[web,mail]" && rm -rf /root/.cache

RUN useradd --create-home --uid 10001 masker
USER masker

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "file_masker.web.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-server-header", "--no-access-log"]
