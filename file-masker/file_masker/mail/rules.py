"""邮件模式的打码规则：按租户，在页面上配置，持久化在 SQLite（state.py）。"""
from __future__ import annotations

import hashlib
import json

from file_masker.engine import build_rules, min_keyword_length
from file_masker.mail.state import StateDb
from file_masker.text import find_residuals

MAX_RULES = 100

RuleItems = tuple[tuple[str, str], ...]  # ((原文, 替换文字), ...)


class RulesError(ValueError):
    """规则不合法；message 可以直接展示给用户。"""


def validate(items: object) -> RuleItems:
    if not isinstance(items, list):
        raise RulesError("Rules must be a list.")
    if len(items) > MAX_RULES:
        raise RulesError(f"At most {MAX_RULES} rules.")
    pairs = []
    for item in items:
        source = item.get("source") if isinstance(item, dict) else None
        target = item.get("target", "") if isinstance(item, dict) else None
        if not isinstance(source, str) or not isinstance(target, str):
            raise RulesError("Each rule needs text for both fields.")
        source = source.strip()
        if not source:
            raise RulesError("Text to mask cannot be empty.")
        if len("".join(source.split())) < min_keyword_length(source):
            raise RulesError(f'"{source}": text to mask needs at least 3 characters (2 for Chinese).')
        pairs.append((source, target))
    rules = build_rules(dict(pairs))
    if len(rules) < len(pairs) or len({r.needle for r in rules}) < len(rules):
        raise RulesError("Duplicate text to mask.")
    for _, target in pairs:
        if find_residuals(target, rules):
            raise RulesError(f'Replacement "{target}" contains a text to mask.')
    return tuple(pairs)


class TenantRules:
    def __init__(self, state: StateDb) -> None:
        self._state = state

    def snapshot(self, tenant: str) -> RuleItems:
        return self._state.get_rules(tenant)

    def replace(self, tenant: str, items: object) -> RuleItems:
        validated = validate(items)
        self._state.replace_rules(tenant, list(validated))
        return validated

    def version(self, tenant: str) -> str:
        """规则内容哈希，写进 mail.json 的 rules_version 便于排查。"""
        items = self.snapshot(tenant)
        if not items:
            return "sha256:" + "0" * 12
        return "sha256:" + hashlib.sha256(json.dumps(items, ensure_ascii=False).encode()).hexdigest()[:12]
