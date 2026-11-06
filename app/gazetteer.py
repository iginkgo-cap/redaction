"""S7 (round11): 英文人名 gazetteer 加载与判定。

数据由 scripts/build_gazetteer.py 从公开源生成（config/gazetteer/*.txt，
禁止手工编辑）；本模块提供两个方向的判定：

- gate 方向（precision.py 用）：单 token 英文 PERSON，`is_common_nonname_word`
  =「常用英文词（google-10000）且非名字且非拼音」→ drop（for/Please/Income/
  million，与生产 FP 分类吻合）。⚠️ 未知罕见 token（Anouk/Hugues/Kek 等多元
  文化人名）**默认保留**——round11 首版曾用「不在名单即 drop」的正向 gate，
  public_en PERSON recall 61→24 崩盘实证方向错误；隐私语义下宁多脱敏不漏。
  名单外的产品名（Obsidian/Yarle 非常用词）由 suppression-list overlay 兜底。
- recall 方向（detectors/gazetteer.py 用）：`recall_eligible` = 已知名
  且**不在歧义表**（May/Will/June 等名字∩高频词），才允许补召回。

拼音音节采用「声母 × 韵母」宽松组合校验（标准普通话音节表是闭集，
组合式校验避免手维护 410 条列表）；宽松度带来的误放行仅作用于
NER 已判 PERSON 的 token，风险受控。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# 音节 = 声母+韵母，或零声母音节（仅标准零声母集，排除裸 o/e/i/u/v ——
# 它们虽是合法汉语音节但从不单独出现在人名拼音里，放开会误放行 mio 类英文词）。
# ue（üe）只跟 n/l/j/q/x/y（nüe/lüe/jue/que/xue/yue），从通用韵母表拆出——
# 否则 t+ue 会把周几缩写 Tue 误判为拼音名。
_PINYIN_SYLLABLE = (
    r"(?:(?:zh|ch|sh|[bpmfdtnlgkhjqxrzcsyw])"
    r"(?:iong|iang|uang|ueng|ang|eng|ong|ian|iao|uai|uan|ing"
    r"|ai|ei|ao|ou|an|en|er|ia|ie|iu|in|ua|uo|ui|un|a|o|e|i|u|v)"
    r"|[nljqxy]ue"
    r"|(?:ang|eng|ai|ei|ao|ou|an|en|er|a))"
)
_PINYIN_NAME = re.compile(rf"(?:{_PINYIN_SYLLABLE}){{1,3}}")


def is_pinyin_name(token: str) -> bool:
    """token（不含分隔符）可拆为 1-3 个合法拼音音节。"""
    t = token.strip().lower()
    if not (2 <= len(t) <= 12):
        return False
    return bool(_PINYIN_NAME.fullmatch(t))


def _load_list(path: Path) -> frozenset[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return frozenset(
                line.strip().lower()
                for line in f
                if line.strip() and not line.startswith("#")
            )
    except FileNotFoundError:
        logger.warning(f"gazetteer 文件缺失: {path}（按空集继续）")
        return frozenset()


class Gazetteer:
    def __init__(self, given: frozenset[str], surnames: frozenset[str],
                 ambiguous: frozenset[str],
                 common_words: frozenset[str] = frozenset()):
        self.given = given
        self.surnames = surnames
        self.ambiguous = ambiguous
        self.common_words = common_words
        self._known = given | surnames

    @classmethod
    def from_dir(cls, gazetteer_dir: Path) -> "Gazetteer":
        return cls(
            given=_load_list(gazetteer_dir / "given_names_en.txt"),
            surnames=_load_list(gazetteer_dir / "surnames_en.txt"),
            ambiguous=_load_list(gazetteer_dir / "ambiguous_names.txt"),
            common_words=_load_list(gazetteer_dir / "common_words_en.txt"),
        )

    def is_known_person_token(self, token: str) -> bool:
        """名单命中或合法拼音即认识（含歧义名）。"""
        t = token.strip().lower()
        return t in self._known or is_pinyin_name(t)

    def is_common_nonname_word(self, token: str) -> bool:
        """gate 方向：常用英文词且非名字且非拼音 → 确定性非人名，可 drop。
        未知罕见 token 返回 False（默认保留，隐私 fail-safe）。"""
        t = token.strip().lower()
        return (
            t in self.common_words
            and t not in self._known
            and not is_pinyin_name(t)
        )

    def recall_eligible(self, token: str) -> bool:
        """recall 方向：仅非歧义的名单名（拼音不参与补召回，FP 面太大）。"""
        t = token.strip().lower()
        return t in self._known and t not in self.ambiguous


_GAZETTEER: Gazetteer | None = None


def get_gazetteer() -> Gazetteer:
    """进程级单例；路径跟随 settings.config_dir（延迟 import 避免环）。"""
    global _GAZETTEER
    if _GAZETTEER is None:
        from .settings import settings

        _GAZETTEER = Gazetteer.from_dir(settings.config_dir / "gazetteer")
    return _GAZETTEER
