"""FastAPI 入口。

启动时 lifespan 钩子加载 HanLP / NVIDIA 模型并预热。
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Response

from .canonical import CanonicalMap
from .detectors import build_detectors
from .merge import apply_context_rules, merge_spans
from .parallel import InferenceLimiter, Overloaded, run_all
from .placeholder import build_redacted, restore
from .schemas import (
    DetectRequest,
    DetectResponse,
    HealthResponse,
    ReadyResponse,
    RedactRequest,
    RedactResponse,
    RestoreRequest,
    RestoreResponse,
    Span,
)
from .settings import settings
from .v2 import api as v2
from .v2.crypto import Cipher, load_keys
from .v2.namespaces import load_registry
from .v2.store import Stores

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("redaction")


_DETECTORS: dict[str, object] = {}
OVERLOADED_CODE = "overloaded"
_ACTIVE_DETECTOR_NAMES: list[str] = []
_LIMITER: InferenceLimiter | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _LIMITER
    if settings.database_url:
        await _open_storage()  # 配了库就必须连上、读到密钥，否则启动失败（ADR-007 §4、§5）
    logger.info("redaction-service starting; loading models ...")
    _LIMITER = InferenceLimiter(settings.max_concurrent_inference, settings.max_pending_requests)
    v2.state.detectors = _detectors_in_order
    v2.state.limiter = _LIMITER
    canon = CanonicalMap(settings.config_dir / "canonical.yaml")
    names = settings.detector_names
    logger.info(f"active detectors (REDACTION_DETECTORS): {names}")
    # registry 按 profile 组装；warmup 失败仅记日志不阻断（与历史行为一致）
    detectors = build_detectors(names, canon, settings, warmup=True)
    _DETECTORS.update(detectors)
    _DETECTORS["canonical"] = canon
    _ACTIVE_DETECTOR_NAMES.clear()
    _ACTIVE_DETECTOR_NAMES.extend(names)
    logger.info("redaction-service ready")
    yield
    if v2.state.stores is not None:
        await v2.state.stores.close()
    v2.state.stores = v2.state.cipher = v2.state.registry = None
    logger.info("redaction-service shutdown")


async def _open_storage() -> None:
    cipher = Cipher(load_keys(settings))
    stores = await Stores.open(settings.database_url, auto_create=settings.database_auto_create)
    registry = load_registry(settings.namespaces_file or settings.config_dir / "namespaces.yaml", stores.removed)
    v2.state.registry, v2.state.stores, v2.state.cipher = registry, stores, cipher
    logger.info(f"mapping storage ready; namespaces={[registry.label(n) for n in sorted(registry.namespaces)]} "
                f"callers={sorted(registry.callers)}")


app = FastAPI(
    title="Ginkgo Redaction Service",
    version="0.1.0",
    lifespan=lifespan,
    # LOOK-6873：接口文档不对外。服务端口可能被绕过反向代理直连，不能只靠 nginx 挡
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
v2.install(app)


def _detectors_in_order() -> list:
    return [_DETECTORS[name] for name in _ACTIVE_DETECTOR_NAMES]


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse()


@app.get("/ready", response_model=ReadyResponse)
async def ready(response: Response) -> ReadyResponse:
    # LOOK-6827：未就绪返回 503，容器健康检查与 curl -f 才能反映模型是否加载完成。
    # Jenkinsfile-mac / scripts/deploy.sh 用 `curl -fsS … | grep '"status":"ready"'` 轮询：503 时 -f 不输出 body，
    # grep 不命中、继续等，就绪后 200 命中——行为与改动前一致。某个 detector 加载失败时健康检查恒为 unhealthy，
    # 这正是要暴露的状态（compose 不会因 unhealthy 重启容器）。
    status_map: dict[str, str] = {}
    if not _ACTIVE_DETECTOR_NAMES:
        response.status_code = 503
        return ReadyResponse(status="loading", detectors=status_map)
    overall = "ready"
    for name in _ACTIVE_DETECTOR_NAMES:
        d = _DETECTORS.get(name)
        if d is None or not d.ready:
            status_map[name] = "loading"
            overall = "loading"
        else:
            status_map[name] = "ready"
    if overall != "ready":
        response.status_code = 503
    return ReadyResponse(status=overall, detectors=status_map)


@app.get("/version")
async def version() -> dict:
    active = list(_ACTIVE_DETECTOR_NAMES)
    settings_out = {
        # GLiNER 主力（knowledgator 等）的活跃旋钮
        "gliner_chunk_chars": settings.gliner_chunk_chars,
        "gliner_overlap": settings.gliner_chunk_overlap,
        "gliner_threshold": settings.gliner_threshold,
        "hanlp_min_zh_chars": settings.hanlp_min_zh_chars,
        "gpu_device": settings.gpu_device,
        "max_concurrent_inference": settings.max_concurrent_inference,
        "max_pending_requests": settings.max_pending_requests,
        "max_text_chars": settings.max_text_chars,
    }
    # 仅当 nvidia 兜底实际在活跃 detector 列表里时，才暴露其 legacy 旋钮
    if "nvidia" in active:
        settings_out["nvidia_chunk_chars"] = settings.nvidia_chunk_chars
        settings_out["nvidia_overlap"] = settings.nvidia_chunk_overlap
        settings_out["nvidia_threshold"] = settings.nvidia_threshold
    return {
        "service": "0.1.0",
        # 每次部署由 deploy 注入的构建戳（git short SHA + UTC 时间戳），
        # 用于确认当前在线容器跑的是哪次构建（部署是否生效）。缺省 dev。
        "build": os.environ.get("BUILD_VERSION", "dev"),
        "detectors": active,
        "storage": "postgres" if v2.state.stores is not None else "none",
        "settings": settings_out,
    }


async def _run_detectors(text: str):
    try:
        return await run_all(
            _detectors_in_order(),
            text,
            timeout_sec=settings.detector_timeout_sec,
            serial=settings.serial_detectors,
            limiter=_LIMITER,
        )
    except Overloaded as exc:
        logger.warning(f"overloaded, rejecting request: {exc}")
        # LOOK-6827：机器码 body 为权威、header 为补充，两者同值；客户端据此降级本地正则
        raise HTTPException(
            503,
            detail={"code": OVERLOADED_CODE, "message": "overloaded, retry later"},
            headers={"X-Redaction-Error": OVERLOADED_CODE, "Retry-After": "1"},
        ) from exc


def _filter_kinds(spans, kinds):
    if not kinds:
        return spans
    allow = set(kinds)
    return [s for s in spans if s["canonical"] in allow]


def _require_v1() -> None:
    if not settings.enable_v1:
        raise HTTPException(status_code=404, detail="Not Found")


@app.post("/v1/detect", response_model=DetectResponse, dependencies=[Depends(_require_v1)])
async def detect(req: DetectRequest) -> DetectResponse:
    t0 = time.time()
    result = await _run_detectors(req.text)
    merged = merge_spans(result.spans)
    merged = apply_context_rules(merged, req.text)
    merged = _filter_kinds(merged, req.kinds)
    elapsed_ms = int((time.time() - t0) * 1000)
    logger.info(
        f"detect chars={len(req.text)} spans={len(merged)} elapsed_ms={elapsed_ms} "
        f"detectors_run={result.run} failed={result.failed} skipped={result.skipped} "
        f"pending={_LIMITER.pending if _LIMITER else -1}"
    )
    return DetectResponse(
        spans=[Span(**s) for s in merged],
        elapsed_ms=elapsed_ms,
        detectors_run=result.run,
        detectors_failed=result.failed,
        detectors_skipped=result.skipped,
    )


@app.post("/v1/redact", response_model=RedactResponse, dependencies=[Depends(_require_v1)])
async def redact(req: RedactRequest) -> RedactResponse:
    t0 = time.time()
    result = await _run_detectors(req.text)
    merged = merge_spans(result.spans)
    merged = apply_context_rules(merged, req.text)
    merged = _filter_kinds(merged, req.kinds)
    redacted, mapping = build_redacted(req.text, merged)
    elapsed_ms = int((time.time() - t0) * 1000)
    return RedactResponse(
        redacted_text=redacted,
        mapping=mapping,
        spans=[Span(**s) for s in merged],
        elapsed_ms=elapsed_ms,
    )


@app.post("/v1/restore", response_model=RestoreResponse, dependencies=[Depends(_require_v1)])
async def restore_endpoint(req: RestoreRequest) -> RestoreResponse:
    out, unresolved = restore(req.text, [m.model_dump() for m in req.mapping])
    return RestoreResponse(text=out, unresolved_placeholders=unresolved)
