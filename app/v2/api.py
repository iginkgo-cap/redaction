"""v2 路由（ADR-002）。main.py 在 lifespan 里填 state，并调用 install(app)。

有库部署的全部接口须带 X-Redaction-Token（ADR-002 §2.1）；无库部署不鉴权、不读 namespace 配置。
"""
from __future__ import annotations

import base64
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

import psycopg
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ..parallel import InferenceLimiter
from ..placeholder import PLACEHOLDER_FMT, PLACEHOLDER_RE
from ..settings import settings
from . import engine, pdf
from .crypto import Cipher
from .detection import Detection, detect_texts
from .errors import ApiError
from .namespaces import Caller, Registry
from .schemas import DetectRequest, LookupRequest, RedactRequest, RemoveRequest, RestoreRequest
from .store import Key, NamespaceStore, Stores, Value

logger = logging.getLogger("redaction.v2")
router = APIRouter(prefix="/v2")


@dataclass
class State:
    detectors: Callable[[], list] = list
    limiter: InferenceLimiter | None = None
    stores: Stores | None = None
    cipher: Cipher | None = None
    registry: Registry | None = None


state = State()


def install(app: FastAPI) -> None:
    app.include_router(router)
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(psycopg.Error, _storage_error)
    app.add_exception_handler(RequestValidationError, _validation_error)


def _error(code: str, message: str) -> JSONResponse:
    err = ApiError(code, message)
    return JSONResponse({"code": code, "message": message}, status_code=err.status, headers=err.headers)


async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
    return _error(exc.code, exc.message)


async def _storage_error(request: Request, exc: psycopg.Error) -> JSONResponse:
    logger.error(f"storage error on {request.url.path}: {exc!r}")
    return _error("storage_unavailable", "mapping storage unavailable, retry later")


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    if not request.url.path.startswith("/v2/"):
        return await request_validation_exception_handler(request, exc)
    msg = "; ".join(f"{'.'.join(map(str, e['loc'][1:]))}: {e['msg']}" for e in exc.errors())
    return _error("invalid_request", msg)


def _version() -> str:
    return os.environ.get("BUILD_VERSION", "dev")


def _caller(request: Request) -> Caller:
    caller = state.registry.caller(request.headers.get("X-Redaction-Token", ""))
    if caller is None:
        raise ApiError("unauthorized", "missing or invalid X-Redaction-Token")
    return caller


def _authorize(request: Request, namespace: str) -> Caller | None:
    """有库部署：校验令牌，且 namespace 已登记并授权给该调用方。无库部署不鉴权，任何 namespace 都接受。"""
    reg = state.registry
    if reg is None:
        return None
    caller = _caller(request)
    if not reg.active(namespace):
        raise ApiError("invalid_request", f"namespace {namespace!r} is not registered")
    if caller.name not in reg.namespaces[namespace].callers:
        # 不在信息里带 slug：别的调用方不该知道这个 id 对应哪个客户
        logger.warning(f"forbidden caller={caller.name} namespace={reg.label(namespace)}")
        raise ApiError("forbidden", f"caller {caller.name} may not access namespace {namespace!r}")
    return caller


def _require_stores() -> None:
    if state.stores is None:
        raise ApiError("storage_unavailable", "no mapping storage configured")


async def _store(namespace: str) -> NamespaceStore:
    ns = state.registry.namespaces[namespace]
    return await state.stores.get(ns.id, ns.slug)


def _who(req: DetectRequest | LookupRequest | RestoreRequest, caller: Caller | None) -> str:
    label = state.registry.label(req.namespace) if state.registry else req.namespace
    return f"namespace={label} caller={caller.name if caller else '-'} source_app={req.source_app}"


def _segments(items: list[dict[str, Any]]) -> list[engine.Segment]:
    segs = engine.flatten(items)
    chars = sum(len(s.text) for s in segs)
    if chars > settings.max_request_chars:
        raise ApiError("text_too_large", f"{chars} chars > {settings.max_request_chars}")
    return segs


async def _detect(segs: list[engine.Segment], req: DetectRequest) -> Detection:
    # 含 file 项时每页一段、段多：每个请求同时只识别 1 段，不挤占在线脱敏的推理槽（LOOK-6876 D8）
    extra = {"segments_in_flight": 1} if any(it.file is not None for it in req.items) else {}
    return await detect_texts(
        [s.text for s in segs],
        level=req.level,
        allow_degrade=req.allow_degrade,
        detectors=state.detectors(),
        limiter=state.limiter,
        timeout_sec=settings.detector_timeout_sec,
        serial=settings.serial_detectors,
        **extra,
    )


async def _extract_files(raw_items: list[dict[str, Any]]) -> dict[int, tuple[bytes, list[pdf.PdfPage]]]:
    """file 项：解码并抽出每页文本。返回 items 下标 → （原字节, 各页）。"""
    out: dict[int, tuple[bytes, list[pdf.PdfPage]]] = {}
    for i, raw in enumerate(raw_items):
        if "file" in raw:
            data = raw["file"].content()
            out[i] = (data, await pdf.extract(data))
    return out


def _page_of(seg: engine.Segment) -> int:
    """pages 项的段 path 为 $.pages[k]，返回页码（1 起）。"""
    return int(seg.path[len("$.pages["):-1]) + 1


def _meta(det: Detection, req: DetectRequest, caller: Caller | None, segs: list[engine.Segment],
          response: Response) -> dict[str, Any]:
    out: dict[str, Any] = {
        "degraded": det.degraded,
        "applied_level": det.applied_level,
        "detectors_failed": det.detectors_failed,
        "version": _version(),
    }
    if det.degraded:
        out["degrade_reason"] = det.degrade_reason
        response.headers["X-Redaction-Degraded"] = "regex"
        logger.warning(
            f"degraded {_who(req, caller)} "
            f"chars={sum(len(s.text) for s in segs)} reason={det.degrade_reason} failed={det.detectors_failed}"
        )
    return out


def _occurrence(req: DetectRequest, seg: engine.Segment, c: dict[str, Any]) -> dict[str, Any]:
    occ: dict[str, Any] = {"item": req.items[seg.item].id}
    if seg.path is not None:
        occ["path"] = seg.path
    occ.update(start=c["start"], end=c["end"])
    return occ


@router.post("/detect")
async def detect(req: DetectRequest, request: Request, response: Response) -> dict[str, Any]:
    caller = _authorize(request, req.namespace)
    if any(it.file is not None for it in req.items):
        raise ApiError("invalid_request", "file items are only supported by /v2/redact")
    segs = _segments([it.as_dict() for it in req.items])
    det = await _detect(segs, req)
    exclude = set(req.exclude.kinds)
    spans: list[list[dict[str, Any]]] = [[] for _ in req.items]
    for seg, found in zip(segs, det.spans):
        cands = [{**s, "priority": engine.DETECTED} for s in found if s["canonical"] not in exclude]
        for s in engine.select(seg.text, cands):
            span: dict[str, Any] = {"start": s["start"], "end": s["end"], "text": s["text"], "kind": s["canonical"],
                                    "source": s["source"], "score": s["score"]}
            if seg.path is not None:
                span["path"] = seg.path
            spans[seg.item].append(span)
    items = [{"id": it.id, "spans": s} for it, s in zip(req.items, spans)]
    return {"items": items, **_meta(det, req, caller, segs, response)}


@router.post("/redact")
async def redact(req: RedactRequest, request: Request, response: Response) -> dict[str, Any]:
    t0 = time.time()
    if state.stores is None:
        raise ApiError("invalid_request", "strategy=placeholder requires mapping storage")
    caller = _authorize(request, req.namespace)
    store, cipher, ns = await _store(req.namespace), state.cipher, req.namespace
    raw_items = [it.as_dict() for it in req.items]
    files = await _extract_files(raw_items)
    # file 项换成每页文本（扫描页 None）进识别；抽出的字数计入 max_request_chars
    flat = [{"pages": [None if p.scanned else p.text for p in files[i][1]]} if i in files else raw
            for i, raw in enumerate(raw_items)]
    segs = _segments(flat)
    det = await _detect(segs, req)

    def key(kind: str, value: str) -> Key:
        return (kind, cipher.digest(ns, kind, engine.norm(value)))

    def value_of(kind: str, text: str, source: str) -> Value:
        return Value(kind, key(kind, text)[1], cipher.encrypt(text), source)

    # 识别结果 → 按 exclude 过滤；false_positive 记下待置抑制
    exclude_kinds = set(req.exclude.kinds)
    exclude_values = {engine.norm(v.value): v for v in req.exclude.values}
    false_positives: dict[Key, Value] = {}
    detected: list[list[dict[str, Any]]] = []
    for found in det.spans:
        cur = []
        for s in found:
            c = {"start": s["start"], "end": s["end"], "text": s["text"], "kind": s["canonical"],
                 "source": s["source"], "score": s["score"], "priority": engine.DETECTED}
            if c["kind"] in exclude_kinds:
                continue
            ex = exclude_values.get(engine.norm(c["text"]))
            if ex is not None:
                if ex.reason == "false_positive":
                    false_positives.setdefault(key(c["kind"], c["text"]), value_of(c["kind"], c["text"], c["source"]))
                continue
            cur.append(c)
        detected.append(cur)

    suppressed = await store.suppressed(list({key(c["kind"], c["text"]) for cur in detected for c in cur}))
    known = await _known(store, req.known_placeholders)
    include = [(v.value, v.kind) for v in req.include.values]

    # 同值扩散（LOOK-6876）：本请求里识别出的值，在所有段的其他出现位置一并替换——模型依赖上下文，
    # 同一个人名常常只认出一处，不补的话文件里会留下明文。按已知值规则找（太短不换、英文按词边界）
    propagated: dict[str, dict[str, Any]] = {}
    for cur in detected:
        for c in cur:
            if key(c["kind"], c["text"]) not in suppressed:
                propagated.setdefault(c["text"], c)

    chosen_per_seg: list[list[dict[str, Any]]] = []
    for seg, cur in zip(segs, detected):
        cands = [c for c in cur if key(c["kind"], c["text"]) not in suppressed]
        for value, c in propagated.items():
            cands += [{**c, "start": s, "end": e} for s, e in engine.find_known(seg.text, value)]
        for ph, kind, value in known:
            cands += [{"start": s, "end": e, "text": value, "kind": kind, "source": "known_placeholders",
                       "score": 1.0, "priority": engine.KNOWN, "placeholder": ph}
                      for s, e in engine.find_known(seg.text, value)]
        for value, kind in include:
            cands += [{"start": s, "end": e, "text": value, "kind": kind, "source": "include",
                       "score": 1.0, "priority": engine.INCLUDE}
                      for s, e in engine.find_all(seg.text, value)]
        chosen_per_seg.append(engine.select(seg.text, cands))

    allocate: dict[Key, Value] = {}
    for chosen in chosen_per_seg:
        for c in chosen:
            if "placeholder" not in c:
                allocate.setdefault(key(c["kind"], c["text"]), value_of(c["kind"], c["text"], c["source"]))
    placeholders = await store.apply(
        suppress=list(false_positives.values()),
        unsuppress=list({key(kind, value) for value, kind in include}),
        allocate=list(allocate.values()),
    )

    entities: dict[str, dict[str, Any]] = {}
    texts: list[str] = []
    # file 项：items 下标 → 页码 → 页内替换；它们的 occurrence 等写回拿到 bbox 后再补
    edits: dict[int, dict[int, list[pdf.Edit]]] = {}
    pending: list[tuple[dict[str, Any], int, int, int]] = []  # (entity, items 下标, 页码, 页内第几处)
    for seg, chosen in zip(segs, chosen_per_seg):
        tokens = []
        for c in chosen:
            ph, new = (c["placeholder"], False) if "placeholder" in c else placeholders[key(c["kind"], c["text"])]
            tokens.append(ph)
            e = entities.setdefault(ph, {"placeholder": ph, "value": c["text"], "kind": c["kind"], "new": new,
                                         "source": c["source"], "score": c["score"], "occurrences": []})
            if seg.item in files:
                page_edits = edits.setdefault(seg.item, {}).setdefault(_page_of(seg), [])
                pending.append((e, seg.item, _page_of(seg), len(page_edits)))
                page_edits.append((c["start"], c["end"], ph))
            else:
                e["occurrences"].append(_occurrence(req, seg, c))
        texts.append(engine.replace(seg.text, chosen, tokens))

    written = {i: await pdf.write(data, edits.get(i, {})) for i, (data, _) in files.items()}
    for e, i, page, k in pending:
        e["occurrences"] += [{"item": req.items[i].id, "page": page, "bbox": box} for box in written[i][1][page][k]]

    items = []
    for i, (it, raw, v) in enumerate(zip(req.items, raw_items, engine.rebuild(flat, segs, texts))):
        if i in files:
            items.append({"id": it.id,
                          "file": {"content_type": it.file.content_type,
                                   "data": base64.b64encode(written[i][0]).decode()},
                          "pages": [{"page": p.page, "scanned": p.scanned} for p in files[i][1]]})
        else:
            items.append({"id": it.id, **({"text": v} if "text" in raw else {"json": v})})
    logger.info(
        f"v2 redact {_who(req, caller)} items={len(items)} segments={len(segs)} "
        f"entities={len(entities)} new={sum(e['new'] for e in entities.values())} "
        f"degraded={det.degraded} elapsed_ms={int((time.time() - t0) * 1000)}"
    )
    return {"items": items, "entities": list(entities.values()), **_meta(det, req, caller, segs, response)}


async def _known(store: NamespaceStore, placeholders: list[str]) -> list[tuple[str, str, str]]:
    found = await store.lookup(list(dict.fromkeys(placeholders)))
    return [(ph, kind, state.cipher.decrypt(enc)) for ph, (kind, enc) in found.items()]


@router.post("/mappings/lookup")
async def lookup(req: LookupRequest, request: Request) -> dict[str, Any]:
    _require_stores()
    _authorize(request, req.namespace)
    store = await _store(req.namespace)
    wanted = list(dict.fromkeys(req.placeholders))
    found = await store.lookup(wanted)
    mappings = [{"placeholder": ph, "value": state.cipher.decrypt(found[ph][1]), "kind": found[ph][0]}
                for ph in wanted if ph in found]
    return {"mappings": mappings, "unresolved": [ph for ph in wanted if ph not in found]}


@router.post("/restore")
async def restore(req: RestoreRequest, request: Request) -> dict[str, Any]:
    _require_stores()
    _authorize(request, req.namespace)
    store = await _store(req.namespace)

    def canonical(m) -> str:  # noqa: ANN001  容忍 LLM 把 _ 转义成 \_
        return PLACEHOLDER_FMT.format(kind=m.group(1), n=m.group(2))

    wanted = sorted({canonical(m) for it in req.items for m in PLACEHOLDER_RE.finditer(it.text)})
    found = await store.lookup(wanted)
    values = {ph: state.cipher.decrypt(enc) for ph, (_, enc) in found.items()}
    unresolved: set[str] = set()

    def sub(m) -> str:  # noqa: ANN001
        ph = canonical(m)
        if ph in values:
            return values[ph]
        unresolved.add(ph)
        return f"[REDACTED:{m.group(1)}]" if req.on_unresolved == "redact_kind" else m.group(0)

    items = [{"id": it.id, "text": PLACEHOLDER_RE.sub(sub, it.text)} for it in req.items]
    return {"items": items, "unresolved": sorted(unresolved)}


@router.post("/namespaces/remove")
async def remove_namespace(req: RemoveRequest, request: Request) -> dict[str, Any]:
    _require_stores()
    # 运维操作，只有 admin 调用方能调：服务端口可能被绕过反向代理直连，不能只靠 nginx 挡
    caller = _caller(request)
    if not caller.admin:
        raise ApiError("forbidden", f"caller {caller.name} may not remove namespaces")
    if req.confirm != req.namespace:
        raise ApiError("invalid_request", "confirm must equal namespace")
    ns = state.registry.namespaces.get(req.namespace)
    if ns is None:
        raise ApiError("invalid_request", f"namespace {req.namespace!r} is not registered")
    removed = await state.stores.remove(ns.id, ns.slug)
    logger.warning(f"namespace removed namespace={state.registry.label(ns.id)} caller={caller.name} entities={removed}")
    return {"namespace": ns.id, "removed_entities": removed}
