"""namespace 与调用方登记（config/namespaces.yaml；ADR-001 D3/D7/D8、ADR-002 §2.1）。

namespace：id 接口里传、永不改，database 名为 rs_<id>；slug 只给人看。
调用方：仓库只存令牌的 SHA-256，请求带 X-Redaction-Token，服务算哈希找到调用方。
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,49}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ConfigError(Exception):
    """namespaces.yaml 不合法，启动即失败。"""


@dataclass(frozen=True)
class Namespace:
    id: str
    slug: str
    callers: frozenset[str]


@dataclass(frozen=True)
class Caller:
    name: str
    admin: bool


class Registry:
    def __init__(self, namespaces: dict[str, Namespace], callers: dict[str, Caller], tokens: dict[str, str],
                 removed: set[str]):
        self.namespaces = namespaces
        self.callers = callers
        self._tokens = tokens  # sha256 hex → 调用方名
        self.removed = removed  # 与 Stores.removed 是同一个集合，删除后立即生效
        for ns in sorted(set(namespaces) & self.removed):
            logger.warning(f"namespace {self.label(ns)} 已删除但仍在 namespaces.yaml 里，按禁用处理；请从配置中移除")

    def caller(self, token: str) -> Caller | None:
        if not token:
            return None
        name = self._tokens.get(hashlib.sha256(token.encode()).hexdigest())
        return self.callers.get(name) if name else None

    def active(self, namespace: str) -> bool:
        return namespace in self.namespaces and namespace not in self.removed

    def label(self, namespace: str) -> str:
        ns = self.namespaces.get(namespace)
        return f"{ns.slug}({namespace})" if ns else namespace


def load_registry(path: Path, removed: set[str]) -> Registry:
    data = yaml.safe_load(path.read_text()) or {}
    callers: dict[str, Caller] = {}
    tokens: dict[str, str] = {}
    for name, c in (data.get("callers") or {}).items():
        callers[name] = Caller(name, bool(c.get("admin", False)))
        for h in c.get("token_sha256") or []:
            if not _SHA256_RE.match(h):
                raise ConfigError(f"caller {name}: token_sha256 must be 64 lowercase hex chars")
            if h in tokens:
                raise ConfigError(f"caller {name}: token hash already used by {tokens[h]}")
            tokens[h] = name
    namespaces: dict[str, Namespace] = {}
    slugs: set[str] = set()
    for n in data.get("namespaces") or []:
        ns_id, slug, allowed = str(n.get("id", "")), str(n.get("slug", "")), list(n.get("callers") or [])
        if not _ID_RE.match(ns_id):
            raise ConfigError(f"namespace id {ns_id!r} must match {_ID_RE.pattern}")
        if not _SLUG_RE.match(slug):
            raise ConfigError(f"namespace {ns_id}: slug {slug!r} must match {_SLUG_RE.pattern}")
        if ns_id in namespaces or slug in slugs:
            raise ConfigError(f"namespace {ns_id}: duplicate id or slug")
        unknown = [c for c in allowed if c not in callers]
        if unknown:
            raise ConfigError(f"namespace {ns_id}: unknown callers {unknown}")
        namespaces[ns_id] = Namespace(ns_id, slug, frozenset(allowed))
        slugs.add(slug)
    return Registry(namespaces, callers, tokens, removed)
