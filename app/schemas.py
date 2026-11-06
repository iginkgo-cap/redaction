"""请求/响应 pydantic schemas。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .settings import settings


# S8 (round11)：kinds 枚举与实际 canonical 输出对齐（原 Literal 10 类陈旧——
# IP/CRYPTO 与实际输出 IP_ADDRESS/CRYPTO_ADDR 不一致，且缺 ACCOUNT 等 5 类，
# 调用方传 kinds 必 422 → labs 侧 fail-closed 放大）。
CANONICAL_KINDS: frozenset[str] = frozenset({
    "PERSON", "PHONE", "EMAIL", "ID_CARD", "BANK_CARD",
    "ACCOUNT", "CRYPTO_ADDR", "ORG", "BUSINESS_ID", "ADDRESS",
    "TIN", "API_KEY", "DOB", "URL", "IP_ADDRESS",
})

# 旧枚举别名向后兼容（曾对外暴露过的两个旧名）
_LEGACY_KIND_ALIASES: dict[str, str] = {"IP": "IP_ADDRESS", "CRYPTO": "CRYPTO_ADDR"}

# 文档用途的类型别名（校验走 _normalize_kinds，接受旧别名并归一）
CanonicalKind = Literal[
    "PERSON", "PHONE", "EMAIL", "ID_CARD", "BANK_CARD",
    "ACCOUNT", "CRYPTO_ADDR", "ORG", "BUSINESS_ID", "ADDRESS",
    "TIN", "API_KEY", "DOB", "URL", "IP_ADDRESS",
]


def _normalize_kinds(v: list[str] | None) -> list[str] | None:
    if v is None:
        return None
    out: list[str] = []
    for k in v:
        norm = _LEGACY_KIND_ALIASES.get(k, k)
        if norm not in CANONICAL_KINDS:
            raise ValueError(
                f"unknown kind: {k!r}; allowed: {sorted(CANONICAL_KINDS)}"
            )
        out.append(norm)
    return out

# 仅作文档参考；Round 9 起 detector 可插拔，故 Span.source 放宽为任意字符串，
# 不再用封闭 Literal 限制（否则新候选 span 会触发 500）。
# Round 9 定稿默认源：cn_regex / en_regex / hanlp / knowledgator（nvidia 退为离线兜底候选）。
SourceName = Literal["cn_regex", "en_regex", "hanlp", "knowledgator", "nvidia"]


class Span(BaseModel):
    start: int
    end: int
    text: str
    canonical: str
    raw_label: str
    source: str  # detector 名（可插拔候选）
    score: float = Field(ge=0.0, le=1.0)


class DetectRequest(BaseModel):
    text: str = Field(max_length=settings.max_text_chars)
    lang_hint: Literal["zh", "en", "auto"] = "auto"
    kinds: list[str] | None = None  # None = 返回全部；旧别名 IP/CRYPTO 自动归一
    # Phase 1 service 端不基于 scope 做识别分支（只透传）；
    # Phase 2 Node pipeline 会用 scope 作 mapping 表分区 key（chat/document/memory/artifact）。
    scope: str | None = None

    _norm_kinds = field_validator("kinds")(_normalize_kinds)


class DetectResponse(BaseModel):
    spans: list[Span]
    elapsed_ms: int
    # LOOK-6827 F'：run = 全部 chunk 成功或按语言阈值整体跳过；failed = 超时 / 抛错 / 有 chunk 失败，
    # 非空时 spans 可能不完整；skipped = 按语言阈值整体跳过（同时在 run 里）。
    detectors_run: list[str]
    detectors_failed: list[str]
    detectors_skipped: list[str]
    version: str = "0.1.0"


class PlaceholderToken(BaseModel):
    placeholder: str
    canonical: str
    original: str
    count: int


class RedactRequest(BaseModel):
    text: str = Field(max_length=settings.max_text_chars)
    lang_hint: Literal["zh", "en", "auto"] = "auto"
    kinds: list[str] | None = None  # 同 DetectRequest：旧别名自动归一

    _norm_kinds = field_validator("kinds")(_normalize_kinds)


class RedactResponse(BaseModel):
    redacted_text: str
    mapping: list[PlaceholderToken]
    spans: list[Span]
    elapsed_ms: int


class RestoreRequest(BaseModel):
    text: str
    mapping: list[PlaceholderToken]


class RestoreResponse(BaseModel):
    text: str
    unresolved_placeholders: list[str] = []


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadyResponse(BaseModel):
    status: Literal["ready", "loading", "error"]
    detectors: dict[str, str]  # name → ready/loading/error
