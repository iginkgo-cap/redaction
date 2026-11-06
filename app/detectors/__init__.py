"""Detector 注册表：name → 工厂。

支持通过 REDACTION_DETECTORS 环境变量按 profile 组装活跃 detector，
让**同一镜像跑不同模型/组合**（Round 9 横评用），无需改代码。

工厂内 **lazy import**：避免仅跑正则 / 单测时就加载 torch / hanlp / gliner。
新增模型 detector（fastino / spacy / gliner_generic 等）时，在此注册一个工厂即可。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from .base import Detector

logger = logging.getLogger(__name__)

# 工厂签名：(canonical_map, settings) -> Detector
DetectorFactory = Callable[[Any, Any], Detector]


def _build_cn_regex(canon: Any, settings: Any) -> Detector:
    from .cn_regex import CnRegexDetector

    return CnRegexDetector(canon)


def _build_en_regex(canon: Any, settings: Any) -> Detector:
    from .en_regex import EnRegexDetector

    return EnRegexDetector(canon, settings.config_dir / "en_regex_rules.yaml")


def _build_hanlp(canon: Any, settings: Any) -> Detector:
    from .hanlp import HanlpDetector

    return HanlpDetector(canon)


def _build_nvidia(canon: Any, settings: Any) -> Detector:
    from .nvidia import NvidiaDetector

    return NvidiaDetector(canon)


def _build_urchade(canon: Any, settings: Any) -> Detector:
    # Round 9 候选：urchade/gliner_multi_pii-v1（base 骨干，Apache-2.0），
    # 作为 NVIDIA 英文 NER 角色的 drop-in（复用 canonical 'nvidia' label）。
    from .gliner_generic import GlinerDetector

    return GlinerDetector(canon, name="urchade", model_repo="urchade/gliner_multi_pii-v1")


def _build_knowledgator(canon: Any, settings: Any) -> Detector:
    # Round 9 选型胜出候选：knowledgator/gliner-pii-base ONNX-INT8（quint8）。
    # 分布外真实金融英文最准(91.5)，Apache，ONNX CPU 快(~60ms/样本)，197MB。
    from .gliner_generic import GlinerDetector

    return GlinerDetector(
        canon, name="knowledgator",
        model_repo="knowledgator/gliner-pii-base-v1.0",
        onnx_file="onnx/model_quint8.onnx",
    )


def _build_spacy(canon: Any, settings: Any) -> Detector:
    # Round 9 非 GLiNER 轻量方案：spaCy en_core_web_lg 统计 NER（英文 PERSON/ORG/ADDRESS）。
    # 与 HanLP（中文）组合即"纯 NLP + 正则、不用大模型"，CPU 上比 GLiNER 快一个数量级。
    from .spacy_ner import SpacyDetector

    return SpacyDetector(canon, name="spacy", model_name="en_core_web_lg")


def _build_presidio(canon: Any, settings: Any) -> Detector:
    # Round 9 非 GLiNER 轻量方案：Presidio（spaCy NER + 内置正则识别器）当英文全栈引擎。
    from .presidio_det import PresidioDetector

    return PresidioDetector(canon, name="presidio", spacy_model="en_core_web_lg")


def _build_spacy_sm(canon: Any, settings: Any) -> Detector:
    # Round 9 调优：更小更快的 en_core_web_sm（~12MB vs lg ~560MB）+ proper-case 护栏。
    from .spacy_ner import SpacyDetector

    return SpacyDetector(canon, name="spacy", model_name="en_core_web_sm",
                         require_proper_case=True)


def _build_spacy_pc(canon: Any, settings: Any) -> Detector:
    # Round 9 调优：en_core_web_lg + proper-case 护栏（隔离护栏对 FP-neg 的效果）。
    from .spacy_ner import SpacyDetector

    return SpacyDetector(canon, name="spacy", model_name="en_core_web_lg",
                         require_proper_case=True)


def _build_presidio_pc(canon: Any, settings: Any) -> Detector:
    # Round 9 调优：Presidio + proper-case 护栏（仅作用 NER 类，降 FP-neg）。
    from .presidio_det import PresidioDetector

    return PresidioDetector(canon, name="presidio", spacy_model="en_core_web_lg",
                            require_proper_case=True)


def _build_spacy_person(canon: Any, settings: Any) -> Detector:
    # Round 9 调优：spaCy 只留 PERSON（ORG 精度仅 21% 是 FP 主凶，ADDRESS 30% 次之）。
    from .spacy_ner import SpacyDetector

    return SpacyDetector(canon, name="spacy", model_name="en_core_web_lg",
                         allowed_canonicals={"PERSON"})


def _build_spacy_pa(canon: Any, settings: Any) -> Detector:
    # Round 9 调优：spaCy 留 PERSON + ADDRESS（砍掉 FP 最重的 ORG）。
    from .spacy_ner import SpacyDetector

    return SpacyDetector(canon, name="spacy", model_name="en_core_web_lg",
                         allowed_canonicals={"PERSON", "ADDRESS"})


# 2026-06-10 英文人名增强候选：spaCy transformer（en_core_web_trf，RoBERTa）只留 PERSON。
# 横评已验证有效（英文 PERSON 召回 42.7→64.6 + 精度反升 71.6 + 真误报零增加），
# 但长文 5000 字 p95 ~25.7s 贴 30s timeout，需配长文分块方案才能上生产。
# 暂不启用，待 D11 决策；结论见 Ginkgo-labs docs/features/redaction/tests/2026-06-10-recognition-eval-and-english-ner.md。
# 重新启用：取消下方函数 + DETECTOR_REGISTRY 中 "spacy_person_trf" 的注释，并 pip install spacy-transformers + en_core_web_trf。
# def _build_spacy_person_trf(canon: Any, settings: Any) -> Detector:
#     from .spacy_ner import SpacyDetector
#     return SpacyDetector(canon, name="spacy", model_name="en_core_web_trf",
#                          allowed_canonicals={"PERSON"})


def _build_gazetteer(canon: Any, settings: Any) -> Detector:
    # S7 (round11)：gazetteer 人名补召回（public_en PERSON recall 61% 缺口）。
    # 名单由 scripts/build_gazetteer.py 从公开源生成（config/gazetteer/）。
    from .gazetteer import GazetteerDetector

    return GazetteerDetector(canon)


DETECTOR_REGISTRY: dict[str, DetectorFactory] = {
    "cn_regex": _build_cn_regex,
    "en_regex": _build_en_regex,
    "gazetteer": _build_gazetteer,
    "hanlp": _build_hanlp,
    "nvidia": _build_nvidia,
    "urchade": _build_urchade,
    "knowledgator": _build_knowledgator,
    "spacy": _build_spacy,
    "presidio": _build_presidio,
    "spacy_sm": _build_spacy_sm,
    "spacy_pc": _build_spacy_pc,
    "presidio_pc": _build_presidio_pc,
    "spacy_person": _build_spacy_person,
    "spacy_pa": _build_spacy_pa,
    # "spacy_person_trf": _build_spacy_person_trf,  # 暂不启用，见上方函数注释（2026-06-10 横评 / 待 D11）
}


def parse_detector_names(raw: str) -> list[str]:
    """逗号分隔 → 去空白、去空项、保持顺序去重。"""
    seen: set[str] = set()
    out: list[str] = []
    for part in (raw or "").split(","):
        name = part.strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def build_detectors(
    names: list[str],
    canon: Any,
    settings: Any,
    *,
    warmup: bool = True,
) -> dict[str, Detector]:
    """按 names 顺序构造 detector，返回 {name: 实例}（保持插入顺序）。

    - 未知 detector 名 → ValueError（启动期 fail-fast，避免静默漏跑）。
    - warmup=True 时对有 warmup() 的 detector 预热；单个预热失败仅记日志、
      不阻断整体（与原 main.py lifespan 行为一致）。
    """
    unknown = [n for n in names if n not in DETECTOR_REGISTRY]
    if unknown:
        raise ValueError(
            f"unknown detector(s): {unknown}; available: {sorted(DETECTOR_REGISTRY)}"
        )
    out: dict[str, Detector] = {}
    for name in names:
        inst = DETECTOR_REGISTRY[name](canon, settings)
        if warmup and hasattr(inst, "warmup"):
            try:
                inst.warmup()
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"detector {name} warmup failed: {exc}")
        out[name] = inst
    return out
