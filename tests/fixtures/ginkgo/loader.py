"""Ginkgo fixtures loader + validator.

加载 e1-e7 JSONL，自动定位 entity span，验证：
1. JSONL 格式合法
2. canonical 在 10 类白名单内
3. 每个 entity.text 在 text 中至少出现一次
4. negatives 中的 token 也在 text 中出现（否则配置无效）
5. cross_turn_refs.appears_in_turns 中每个 turn 的 text 确实含 ref.text

提供 `load_fixtures()` 给 pytest 评测用。
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from typing import Iterable

CANONICAL_KINDS: set[str] = {
    # 原 10 类
    "PERSON", "PHONE", "EMAIL", "ID_CARD", "BANK_CARD",
    "ACCOUNT", "CRYPTO_ADDR", "ORG", "BUSINESS_ID", "ADDRESS",
    # T11 新增 3 类（参考 Gretel / BigCode PII 类别）
    "TIN",       # 税务识别号（CRS / FATCA 上下文）
    "API_KEY",   # API 凭证（OpenAI sk- / AWS AKIA / GitHub ghp_ / Lark/Slack token）
    "DOB",       # 出生日期（从 ID_CARD 抽出单独标）
    # Round 11（S8）对齐 app/schemas.py CANONICAL_KINDS 全集（15 类）
    "URL",         # S5 后仅敏感特征 URL（userinfo / 凭据 query / 内网 host）
    "IP_ADDRESS",  # D11 回归的 IPv4
}

FIXTURE_DIR = pathlib.Path(__file__).parent


@dataclass
class Span:
    start: int
    end: int
    text: str
    canonical: str
    note: str = ""


@dataclass
class Entity:
    text: str
    canonical: str
    note: str = ""
    occurrence_index: int | None = None  # None = 取所有匹配


@dataclass
class SingleSample:
    id: str
    scope: str
    language: str
    text: str
    entities: list[Entity] = field(default_factory=list)
    negatives: list[str] = field(default_factory=list)

    def resolve_spans(self) -> list[Span]:
        """Resolve entities to concrete spans by text.find()."""
        out: list[Span] = []
        for ent in self.entities:
            matches = _find_all(self.text, ent.text)
            if not matches:
                raise ValueError(
                    f"[{self.id}] entity {ent.text!r} ({ent.canonical}) not found in text"
                )
            if ent.occurrence_index is not None:
                matches = [matches[ent.occurrence_index]]
            for s, e in matches:
                out.append(Span(s, e, ent.text, ent.canonical, ent.note))
        return out


@dataclass
class Turn:
    turn_id: int
    role: str
    text: str
    entities: list[Entity] = field(default_factory=list)


@dataclass
class CrossTurnRef:
    text: str
    canonical: str
    appears_in_turns: list[int]
    note: str = ""


@dataclass
class MultiTurnSample:
    id: str
    scope: str
    language: str
    turns: list[Turn]
    cross_turn_refs: list[CrossTurnRef] = field(default_factory=list)


def _find_all(haystack: str, needle: str) -> list[tuple[int, int]]:
    if not needle:
        return []
    out: list[tuple[int, int]] = []
    start = 0
    n = len(needle)
    while True:
        idx = haystack.find(needle, start)
        if idx < 0:
            break
        out.append((idx, idx + n))
        start = idx + 1  # 允许重叠匹配；evaluator 决定是否去重
    return out


def _parse_entity(d: dict) -> Entity:
    canonical = d["canonical"]
    if canonical not in CANONICAL_KINDS:
        raise ValueError(f"unknown canonical: {canonical}")
    return Entity(
        text=d["text"],
        canonical=canonical,
        note=d.get("note", ""),
        occurrence_index=d.get("occurrence_index"),
    )


def _parse_single(d: dict) -> SingleSample:
    return SingleSample(
        id=d["id"],
        scope=d["scope"],
        language=d["language"],
        text=d["text"],
        entities=[_parse_entity(e) for e in d.get("entities", [])],
        negatives=list(d.get("negatives", [])),
    )


def _parse_multi(d: dict) -> MultiTurnSample:
    turns = [
        Turn(
            turn_id=t["turn_id"],
            role=t["role"],
            text=t["text"],
            entities=[_parse_entity(e) for e in t.get("entities", [])],
        )
        for t in d["turns"]
    ]
    refs = [
        CrossTurnRef(
            text=r["text"],
            canonical=r["canonical"],
            appears_in_turns=r["appears_in_turns"],
            note=r.get("note", ""),
        )
        for r in d.get("cross_turn_refs", [])
    ]
    return MultiTurnSample(
        id=d["id"],
        scope=d["scope"],
        language=d["language"],
        turns=turns,
        cross_turn_refs=refs,
    )


def load_single(path: pathlib.Path) -> list[SingleSample]:
    out: list[SingleSample] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            out.append(_parse_single(json.loads(line)))
    return out


def load_multi(path: pathlib.Path) -> list[MultiTurnSample]:
    out: list[MultiTurnSample] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            out.append(_parse_multi(json.loads(line)))
    return out


SINGLE_FILES = [
    "e1_ic_memo.jsonl",
    "e2_remittance_email.jsonl",
    "e3_kyc_beneficiary.jsonl",
    "e5_contract.jsonl",
    "e6_crypto_holding.jsonl",
    "e7_offshore.jsonl",
    "e8_internal_systems.jsonl",
    "e9_long_documents.jsonl",
    "e10_ocr_variants.jsonl",
    "e11_medium_500_800.jsonl",
    "e12_medium_800_1500.jsonl",
    "e13_long_1500_2500.jsonl",
    "e14_long_2500_3500.jsonl",
    "e15_finance_en.jsonl",  # Round 9 新增：金融英文场景集
    "e16_production_negatives.jsonl",  # Round 11 新增：生产误报回归集（合成等价样本）
    "e17_goodcase_guards.jsonl",  # Round 11 新增：good case 守护集（规则边界上的真敏感值，防过杀）
]
MULTI_FILES = ["e4_lark_multiturn.jsonl"]


def load_all() -> tuple[list[SingleSample], list[MultiTurnSample]]:
    singles: list[SingleSample] = []
    for fname in SINGLE_FILES:
        p = FIXTURE_DIR / fname
        if p.exists():
            singles.extend(load_single(p))
    multis: list[MultiTurnSample] = []
    for fname in MULTI_FILES:
        p = FIXTURE_DIR / fname
        if p.exists():
            multis.extend(load_multi(p))
    return singles, multis


def load_dir(directory: pathlib.Path) -> tuple[list[SingleSample], list[MultiTurnSample]]:
    """加载任意目录下所有 *.jsonl，按首行是否含 'turns' 字段区分 single / multi。

    用于评测 ginkgo 之外的 fixture（如 public_en / public_zh），与 evaluate
    的 --fixture-dir 配合。
    """
    singles: list[SingleSample] = []
    multis: list[MultiTurnSample] = []
    for path in sorted(directory.glob("*.jsonl")):
        is_multi = False
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("//"):
                    continue
                try:
                    is_multi = "turns" in json.loads(line)
                except json.JSONDecodeError:
                    pass
                break
        if is_multi:
            multis.extend(load_multi(path))
        else:
            singles.extend(load_single(path))
    return singles, multis


def validate_all() -> list[str]:
    """Returns list of error messages; empty list = all clean.

    新增 4 条防回归检查（T4）：
    ① 同 sample 内 entity span 互相 overlap → 报错（catch substring 幽灵 GT bug）
    ② 跨样本同字面值不同 canonical → 警告（防误标，区分 USCC vs ID_CARD 等）
    ③ cross_turn_refs.text 在每个 appears_in_turns 内必须有对应 entity → 警告
    ④ negatives 与 entity 文本完全重合 → 报错（语义冲突）
    """
    errors: list[str] = []
    singles, multis = load_all()

    seen_ids: set[str] = set()
    # ②：跨样本字面值 → canonical 集合
    text_to_canon: dict[str, dict[str, set[str]]] = {}

    for s in singles:
        if s.id in seen_ids:
            errors.append(f"duplicate id: {s.id}")
        seen_ids.add(s.id)
        try:
            spans = s.resolve_spans()
        except ValueError as e:
            errors.append(str(e))
            spans = []

        # ① span 互相 overlap 检查（按 start 排序后两两比较）
        sorted_spans = sorted(spans, key=lambda x: (x.start, x.end))
        for a, b in zip(sorted_spans, sorted_spans[1:]):
            if b.start < a.end:
                errors.append(
                    f"[{s.id}] entity span overlap: {a.text!r}({a.canonical}, {a.start}-{a.end}) "
                    f"vs {b.text!r}({b.canonical}, {b.start}-{b.end}) — 用 occurrence_index 排除"
                )

        # ② 累积跨样本字面值 → canonical 映射
        for ent in s.entities:
            entry = text_to_canon.setdefault(ent.text, {"canonicals": set(), "samples": set()})
            entry["canonicals"].add(ent.canonical)
            entry["samples"].add(s.id)

        # ④ negatives 与 entity 文本完全重合
        ent_texts = {e.text for e in s.entities}
        for neg in s.negatives:
            if neg not in s.text:
                errors.append(f"[{s.id}] negative {neg!r} not in text")
            if neg in ent_texts:
                errors.append(
                    f"[{s.id}] negative {neg!r} 与 entity 文本完全重合（同时被标 PII 又被标公开）"
                )

    for m in multis:
        if m.id in seen_ids:
            errors.append(f"duplicate id: {m.id}")
        seen_ids.add(m.id)
        turns_by_id: dict[int, Turn] = {}
        for t in m.turns:
            turns_by_id[t.turn_id] = t
            for ent in t.entities:
                if ent.text not in t.text:
                    errors.append(
                        f"[{m.id} turn {t.turn_id}] entity {ent.text!r} not in turn text"
                    )
                # ② 多轮也累积
                entry = text_to_canon.setdefault(
                    ent.text, {"canonicals": set(), "samples": set()}
                )
                entry["canonicals"].add(ent.canonical)
                entry["samples"].add(f"{m.id}#t{t.turn_id}")
        for ref in m.cross_turn_refs:
            if ref.canonical not in CANONICAL_KINDS:
                errors.append(f"[{m.id}] cross_turn_ref unknown canonical {ref.canonical}")
            for tid in ref.appears_in_turns:
                if tid not in turns_by_id:
                    errors.append(f"[{m.id}] cross_turn_ref points to missing turn {tid}")
                    continue
                turn = turns_by_id[tid]
                if ref.text not in turn.text:
                    errors.append(
                        f"[{m.id} turn {tid}] cross_turn_ref {ref.text!r} not in turn text"
                    )
                    continue
                # ③ ref 必须有对应 entity 标注（不能 ref 个连 GT 都没标的字面值）
                if not any(
                    e.text == ref.text and e.canonical == ref.canonical for e in turn.entities
                ):
                    errors.append(
                        f"[{m.id} turn {tid}] cross_turn_ref {ref.text!r}({ref.canonical}) "
                        f"在该 turn 的 entities 中没有对应标注"
                    )

    # ② 报告跨样本不一致
    for text, info in text_to_canon.items():
        if len(info["canonicals"]) > 1:
            errors.append(
                f"跨样本 canonical 不一致：{text!r} 被标为 {sorted(info['canonicals'])} "
                f"出现在 {sorted(info['samples'])[:5]}（如确实是同名不同物，需在 note 内说明并按 occurrence_index 区分）"
            )

    return errors


def summarize() -> dict:
    singles, multis = load_all()
    from collections import Counter

    counter: Counter[str] = Counter()
    by_scope: Counter[str] = Counter()
    total_entities = 0
    for s in singles:
        by_scope[s.scope] += 1
        for ent in s.entities:
            counter[ent.canonical] += 1
            total_entities += 1
    for m in multis:
        by_scope[m.scope] += 1
        for t in m.turns:
            for ent in t.entities:
                counter[ent.canonical] += 1
                total_entities += 1
    return {
        "single_samples": len(singles),
        "multi_samples": len(multis),
        "multi_total_turns": sum(len(m.turns) for m in multis),
        "total_entities": total_entities,
        "by_canonical": dict(counter),
        "by_scope": dict(by_scope),
    }


if __name__ == "__main__":
    import sys

    errors = validate_all()
    if errors:
        print(f"[FAIL] {len(errors)} errors:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    summary = summarize()
    print("[OK] All fixtures valid.")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
