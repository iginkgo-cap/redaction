"""namespaces.yaml：id / slug / 调用方令牌的加载与校验。"""
from __future__ import annotations

import hashlib

import pytest

from app.v2.namespaces import ConfigError, load_registry


def _sha(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def _load(tmp_path, text: str, removed=frozenset()):
    f = tmp_path / "namespaces.yaml"
    f.write_text(text)
    return load_registry(f, set(removed))


GOOD = f"""
callers:
  labs: {{token_sha256: ["{_sha('t1')}", "{_sha('t1-new')}"]}}
  ops: {{token_sha256: ["{_sha('t2')}"], admin: true}}
namespaces:
  - {{id: 9f2c0000-aaaa-bbbb-cccc-000000000001, slug: acme, callers: [labs]}}
  - {{id: old, slug: old-co, callers: [labs]}}
"""


def test_令牌按哈希匹配_支持轮换(tmp_path) -> None:
    reg = _load(tmp_path, GOOD)
    assert reg.caller("t1").name == "labs" and reg.caller("t1-new").name == "labs"
    assert reg.caller("t2").admin is True
    assert reg.caller("wrong") is None and reg.caller("") is None


def test_label_与墓碑(tmp_path) -> None:
    reg = _load(tmp_path, GOOD, removed={"old"})
    assert reg.label("9f2c0000-aaaa-bbbb-cccc-000000000001") == "acme(9f2c0000-aaaa-bbbb-cccc-000000000001)"
    assert reg.active("9f2c0000-aaaa-bbbb-cccc-000000000001")
    assert not reg.active("old") and not reg.active("unknown")


@pytest.mark.parametrize("text", [
    "namespaces: [{id: Bad_Upper, slug: a}]",
    "namespaces: [{id: a, slug: Bad}]",
    "namespaces: [{id: a, slug: x}, {id: b, slug: x}]",
    "namespaces: [{id: a, slug: x}, {id: a, slug: y}]",
    "namespaces: [{id: a, slug: x, callers: [nobody]}]",
    "callers: {c: {token_sha256: [NOTHEX]}}",
    f"callers: {{a: {{token_sha256: ['{_sha('t')}']}}, b: {{token_sha256: ['{_sha('t')}']}}}}",
])
def test_配置不合法启动失败(tmp_path, text) -> None:
    with pytest.raises(ConfigError):
        _load(tmp_path, text)

