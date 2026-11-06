"""schemas.py 单测（round11 S8 kinds 枚举对齐）。

背景：原 Literal 10 类陈旧（IP/CRYPTO 与实际输出不符、缺 5 类），
调用方传 kinds 必 422 → labs 侧 fail-closed 放大。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import CANONICAL_KINDS, DetectRequest, RedactRequest


def test_全部15类canonical_可用() -> None:
    req = DetectRequest(text="x", kinds=sorted(CANONICAL_KINDS))
    assert set(req.kinds) == CANONICAL_KINDS


def test_新增5类_不再422() -> None:
    # 原枚举缺失的类：ACCOUNT / BUSINESS_ID / TIN / API_KEY / DOB
    req = DetectRequest(text="x", kinds=["ACCOUNT", "BUSINESS_ID", "TIN", "API_KEY", "DOB"])
    assert req.kinds == ["ACCOUNT", "BUSINESS_ID", "TIN", "API_KEY", "DOB"]


def test_旧别名_IP_CRYPTO_自动归一() -> None:
    req = DetectRequest(text="x", kinds=["IP", "CRYPTO"])
    assert req.kinds == ["IP_ADDRESS", "CRYPTO_ADDR"]


def test_未知kind_报错() -> None:
    with pytest.raises(ValidationError):
        DetectRequest(text="x", kinds=["NOT_A_KIND"])


def test_kinds_None_全量() -> None:
    assert DetectRequest(text="x").kinds is None


def test_redact_请求同样归一() -> None:
    req = RedactRequest(text="x", kinds=["IP"])
    assert req.kinds == ["IP_ADDRESS"]


def test_text_超过上限_422() -> None:
    from app.settings import settings

    DetectRequest(text="x" * settings.max_text_chars)
    with pytest.raises(ValidationError):
        DetectRequest(text="x" * (settings.max_text_chars + 1))
