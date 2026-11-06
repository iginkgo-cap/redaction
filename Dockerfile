# syntax=docker/dockerfile:1.6
# Ginkgo Redaction Service
# 多阶段构建 — Phase 1 实现，与 docs/features/redaction/2026-05-19-pii-redaction-implementation.md §1.3 对齐
#
# 注意：模型（knowledgator-base ONNX-INT8 ~198MB，可选 HanLP ~500MB）不打包进 image，
# 通过 volume 挂载在运行时加载（Round 9 D9：英文/多语 NER 主力由
# nvidia gliner-PII ~1.7GB 换为 knowledgator quint8 最小集 ~198MB）。下载见 scripts/download_models.py。
#
# 构建参数（LOOK-5231）：
#   WITH_HANLP=1   额外安装 HanLP（模型 CC BY-NC-SA 4.0 非商用，只给内部部署用；对外镜像不带）
#   PIP_INDEX / PIP_TRUSTED_HOST   默认官方 PyPI；国内构建传阿里云镜像（见 Jenkinsfile-mac）

ARG PYTHON_VERSION=3.11.9

# ---------- builder：构建依赖 wheel ----------
FROM python:${PYTHON_VERSION}-slim AS builder
WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# pip 索引：默认官方 PyPI；中国境内构建传 --build-arg PIP_INDEX=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_INDEX=https://pypi.org/simple/
ARG PIP_TRUSTED_HOST=pypi.org
ARG WITH_HANLP=0

# 先装 CPU-only PyTorch（avoid 6GB CUDA libs in Linux image）
# 走官方 pytorch CPU 索引（torch 镜像在 aliyun 上不存在；torch 大文件 + CDN 大概率没问题）
RUN pip wheel --wheel-dir /wheels --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cpu \
    --trusted-host download.pytorch.org \
    "torch>=2.4.0"

# 再装其他依赖（torch 已在 /wheels 里，pip 会优先用）；
# 主索引走 PIP_INDEX，extra-index 兜底 pytorch CPU 索引（防止其他包要 pytorch 依赖）
COPY requirements.txt requirements-hanlp.txt ./
RUN EXTRA=""; if [ "${WITH_HANLP}" = "1" ]; then EXTRA="-r requirements-hanlp.txt"; fi; \
    pip wheel --wheel-dir /wheels --no-cache-dir \
    --index-url ${PIP_INDEX} \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    --trusted-host ${PIP_TRUSTED_HOST} \
    --trusted-host download.pytorch.org \
    --timeout 120 --retries 5 \
    -r requirements.txt ${EXTRA}

# file-masker 引擎（LOOK-6876）：本仓库子目录，服务只用 engine；--no-deps 不带 [web,mail]，pymupdf 已在 requirements.txt
COPY file-masker/pyproject.toml file-masker/README.md file-masker/LICENSE ./file-masker/
COPY file-masker/file_masker/ ./file-masker/file_masker/
RUN pip wheel --wheel-dir /wheels --no-cache-dir --no-deps \
    --index-url ${PIP_INDEX} --trusted-host ${PIP_TRUSTED_HOST} \
    ./file-masker

# ---------- runtime：最小运行环境 ----------
FROM python:${PYTHON_VERSION}-slim AS runtime
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 装依赖：BuildKit bind mount 让 wheels 不进 runtime 层（省 ~3GB image size）
ARG WITH_HANLP=0
COPY requirements.txt requirements-hanlp.txt ./
RUN --mount=type=bind,from=builder,source=/wheels,target=/wheels \
    EXTRA=""; if [ "${WITH_HANLP}" = "1" ]; then EXTRA="-r requirements-hanlp.txt"; fi; \
    pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt ${EXTRA} file-masker

# 业务代码与配置
COPY app/ ./app/
COPY config/ ./config/
COPY scripts/ ./scripts/

# 模型挂载点（volume mount target）
RUN mkdir -p /root/.hanlp /root/.cache/huggingface

# 每次部署注入的构建版本戳（deploy 传 --build-arg；/version 暴露，用于确认部署生效）。
# 置于 COPY 之后：代码层已使缓存失效，此 ARG 变更不额外破坏 pip 依赖层缓存。
ARG BUILD_VERSION=dev

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    REDACTION_CONFIG_DIR=/app/config \
    HF_HOME=/root/.cache/huggingface \
    HANLP_HOME=/root/.hanlp \
    BUILD_VERSION=${BUILD_VERSION}

EXPOSE 3006

HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=3 \
    CMD curl -fsS http://localhost:3006/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3006", \
     "--workers", "1", "--proxy-headers"]
