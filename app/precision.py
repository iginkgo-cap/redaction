"""S 系列精度后处理（round11）：值抑制名单 + 垃圾 span 结构规则 + 上下文 gate。

数据依据：2026-07-02 线上误报统计（已识别实体 387 条中 ~58% 为噪音）+ 182 条用户取消识别的记录。

挂载点：merge.apply_context_rules 尾部（_filter_kinds 之前），
对 /v1/detect 与 /v1/redact 同时生效。

规则清单：
- S1 SuppressionList  值抑制名单（config/suppression-list.yaml，收编原 F7 ORG_PUBLIC）
- S2 _is_person_junk  PERSON 结构垃圾（超长/句读/单字/纯小写/缩写/称谓/公司后缀）
- S3 _is_address_junk ADDRESS 粒度门控（纯数字/无结构要素短地名）
- S4 _dob_allowed     DOB 出生引导词 gate + 未来年份拒识
- S6 _bic_allowed     SWIFT_BIC 上下文词 + ISO 3166 国家码双 gate
- S7 _single_token_person_allowed  单 token 英文 PERSON gazetteer gate
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any, Sequence

import yaml

logger = logging.getLogger(__name__)


def _normalize_value(s: str) -> str:
    """NFKC 归一 + strip + lower（沿用原 F7 _normalize_org 口径）。
    覆盖全角↔半角（ＨＳＢＣ → HSBC）；繁简差异不在 NFKC 范畴，暂不处理。"""
    return unicodedata.normalize("NFKC", s).strip().lower()


class SuppressionList:
    """S1 全局值抑制名单：命中（同 kind 段或 "*" 段，精确匹配）即 drop 不脱敏。

    yaml 结构：suppress: {<CANONICAL_KIND>|"*": [value, ...]}
    维护规则见 config/suppression-list.yaml 头注释（基座层公开源 / overlay 层生产增量）。
    """

    def __init__(self, by_kind: dict[str, set[str]] | None = None):
        self._by_kind: dict[str, set[str]] = by_kind or {}

    @classmethod
    def from_yaml(cls, path: Path, overlays: Sequence[Path] = ()) -> "SuppressionList":
        """主名单 + 可选补充名单（存在才合并，缺失不告警）。LOOK-5231：内部专用条目放补充名单，不外发。"""
        by_kind: dict[str, set[str]] = {}
        for p, required in [(path, True), *((o, False) for o in overlays)]:
            try:
                with open(p, encoding="utf-8") as f:
                    data: dict[str, Any] = yaml.safe_load(f) or {}
            except FileNotFoundError:
                if required:
                    logger.warning(f"suppression list 缺失: {p}（按空名单继续）")
                continue
            for kind, values in (data.get("suppress") or {}).items():
                by_kind.setdefault(str(kind), set()).update(_normalize_value(str(v)) for v in (values or []))
        return cls(by_kind)

    def hit(self, canonical: str, text: str) -> bool:
        norm = _normalize_value(text)
        if norm in self._by_kind.get(canonical, ()):
            return True
        return norm in self._by_kind.get("*", ())


_SUPPRESSION: SuppressionList | None = None


def get_suppression_list() -> SuppressionList:
    """进程级单例；配置路径跟随 settings.config_dir（延迟 import 避免环）。"""
    global _SUPPRESSION
    if _SUPPRESSION is None:
        from .settings import settings

        _SUPPRESSION = SuppressionList.from_yaml(
            settings.config_dir / "suppression-list.yaml",
            overlays=[settings.config_dir / "suppression-list.internal.yaml"],
        )
    return _SUPPRESSION


# ── S2 PERSON 结构垃圾 ────────────────────────────────────────────────
# 生产字典 223 条 PERSON 中 ~90 条命中本组（整句 span / 功能词 / 缩写 / 称谓）。
_PERSON_MAX_CHARS = 30
_PERSON_MAX_WORDS = 4
# 超限宽免的硬上限：8 词以上不可能是单人姓名，不再做名单复核。
_PERSON_MAX_WORDS_HARD = 8
# 姓名小词（西语/荷语/阿语等联接词）：不参与「常用词即句子」的判定。
_NAME_PARTICLES = frozenset({
    "de", "la", "le", "du", "da", "di", "del", "della", "dos", "das", "do",
    "van", "von", "der", "den", "ter", "ten", "te", "bin", "binti", "binte",
    "al", "el", "abu", "ibn", "st",
})
_PROPER_TOKEN = re.compile(r"^[A-Z][a-z'\-]+$")
_LATIN_NAME_TOKEN = re.compile(r"^[A-Za-z][A-Za-z'\-]*\.?$")
_CJK_NAME_2_4 = re.compile(r"^[一-鿿]{2,4}$")
_CJK_DOTTED_NAME = re.compile(r"^[一-鿿]{1,6}(?:·[一-鿿]{1,6}){1,3}$")
_CJK_SHORT_TOKEN = re.compile(r"^[一-鿿]{1,4}$")
# 常用中文姓氏（闭集：百家姓头部 + 常见复姓，文化常量非"死名单"，
# 仅作 salvage 子段的姓名实证，不做否决——首字非表内姓氏只是不抢救，
# 不影响 NER 正常识别路径）。
_CN_SURNAMES = frozenset(
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何林罗高郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜"
    "叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦傅方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎"
    "贺顾毛郝龚邵万钱严覃武戴莫孔向汤温康施文柯倪"
)
_CN_COMPOUND_SURNAMES = (
    "欧阳", "司马", "诸葛", "上官", "司徒", "东方", "独孤", "慕容",
    "夏侯", "皇甫", "尉迟", "令狐",
)
# 句读/结构符号命中即非人名。保留合法人名符号：连字符（Jean-Pierre）、
# 撇号（O'Brien）、间隔号（迈克尔·乔丹）、缩写点（J. K. Rowling）。
_PERSON_JUNK_CHARS = re.compile(
    r'[，。：；！？、…\n\t\r*&#@%=+_/\\|<>(){}\[\]"「」『』【】§]'
)
# 全大写 2-4 字母 = 货币/交易所/机构缩写（HKD/HKEX/KPMG/CRS/ZT/WTZ），非人名。
_PERSON_ACRONYM = re.compile(r"^[A-Z]{2,4}$")
# 中文称谓：姓 + 职务/敬称。fullmatch 保证「张总统」「王老师傅」不误杀。
_PERSON_CN_TITLE = re.compile(
    r"^[一-鿿](?:总|经理|老师|先生|女士|董事|主任|哥|姐)$"
)
# 英文称谓 + 单姓（Mr. Wang / Ms Smith）：单姓无定位到个人的能力，
# 且生产 exclusion 显示用户反复取消此类识别（张总 ×4 / Mr.Wang ×2）。
_PERSON_EN_TITLE = re.compile(
    r"^(?:Mr|Ms|Mrs|Dr|Miss|Sir|Madam)\.?\s+[A-Za-z][A-Za-z.\-']*$",
    re.IGNORECASE,
)
# 公司后缀结尾（International Inc. / XX Ltd）= ORG 误标 PERSON。
_PERSON_ORG_SUFFIX = re.compile(
    r"\b(?:inc|ltd|llc|plc|corp|co|group|holdings|limited|company)\.?$",
    re.IGNORECASE,
)
_HAS_UPPER_OR_CJK = re.compile(r"[A-Z一-鿿]")
# 人名不含数字（Section 2 / 3号楼 类误报）。
_HAS_DIGIT = re.compile(r"\d")
# 单 token 内中英文字符混杂且较长（enex在这张截图里的ready）= NER span 溢出，
# 非规范人名。⚠️ 仅判定**同一空格分隔 token 内**的混杂——「Patrick 黄」这类
# 英文名+中文姓（token 各自纯script）是合法人名，round11 评测跨轮 case 实证，
# 不得误杀；短混杂昵称（阿May / 李Peter）也放行，阈值 8。
_CJK_LATIN_MIXED_MIN = 8
_HAS_CJK = re.compile(r"[一-鿿]")
_HAS_LATIN = re.compile(r"[A-Za-z]")


# 混杂 token 宽免：「1-2 个 CJK 姓 + 已知英文名」（李Michael / 陈Christina）
# 或反序（Michael李）是合法混排人名，与长度阈值无关（review P2 修订）。
_MIXED_CJK_LATIN_NAME = re.compile(
    r"^(?:[一-鿿]{1,2}([A-Za-z][A-Za-z'\-]+)|([A-Za-z][A-Za-z'\-]+)[一-鿿]{1,2})$"
)


def _has_mixed_script_token(text: str) -> bool:
    for tok in text.split():
        if (
            len(tok) >= _CJK_LATIN_MIXED_MIN
            and _HAS_CJK.search(tok)
            and _HAS_LATIN.search(tok)
        ):
            m = _MIXED_CJK_LATIN_NAME.fullmatch(tok)
            if m:
                from .gazetteer import get_gazetteer

                latin = m.group(1) or m.group(2)
                if get_gazetteer().is_known_person_token(latin):
                    continue
            return True
    return False


def _plausible_long_name(t: str) -> bool:
    """超长/超词数 span 的姓名复核（review P1 修订）：西语/南亚等长姓名
    （Juan Carlos de la Cruz / Krishnamurthy Venkatasubramanian）不得整 span
    误杀。每个 token 须为：已知名 / 姓名小词 / CJK 短 token / 首字母大写且
    非常用词（未知罕见名 fail-safe 保留，与 S7 负向 gate 同向）。
    句子必含常用词或小写非名 token，天然不过此复核。"""
    toks = t.split()
    if len(toks) > _PERSON_MAX_WORDS_HARD:
        return False
    from .gazetteer import get_gazetteer

    gaz = get_gazetteer()
    for tok in toks:
        if _CJK_SHORT_TOKEN.fullmatch(tok) or _CJK_DOTTED_NAME.fullmatch(tok):
            continue
        low = tok.rstrip(".").lower()
        if low in _NAME_PARTICLES:
            continue
        if gaz.is_known_person_token(low):
            continue
        if _PROPER_TOKEN.fullmatch(tok) and not gaz.is_common_nonname_word(low):
            continue
        return False
    return True


def _is_person_junk(text: str) -> bool:
    t = text.strip()
    if len(t) <= 1:  # 单字姓氏碎片（李/张/王）/ 单字母
        return True
    if (
        len(t) > _PERSON_MAX_CHARS or len(t.split()) > _PERSON_MAX_WORDS
    ) and not _plausible_long_name(t):
        return True
    if _PERSON_JUNK_CHARS.search(t):
        return True
    if not _HAS_UPPER_OR_CJK.search(t):
        # 纯小写多为误报（for / vs / my grammar / an / de——生产字典实证）。
        # 唯一宽免：**多 token 且全部为已知名**（脏文本里的小写全名
        # "john smith" / "yan xuan"）；单 token 小写一律杀（million/figure/
        # lili 这类单词即使撞上姓氏名单/拼音也是误报主场）
        toks = t.split()
        if len(toks) < 2:
            return True
        from .gazetteer import get_gazetteer

        gaz = get_gazetteer()
        if not all(gaz.is_known_person_token(tok) for tok in toks):
            return True
    if _PERSON_ACRONYM.fullmatch(t):
        # 全大写单姓宽免（review P2）：KYC 表单体（Surname: WANG / CHEN / NG）。
        # 已知名（名单∪拼音）→ 保留；HKD/HKEX/KPMG/CRS 等缩写均非已知名照杀。
        # 唯一已知碰撞「OR」（Or 为罕见姓）走 suppression overlay。
        from .gazetteer import get_gazetteer

        if not get_gazetteer().is_known_person_token(t):
            return True
    if _PERSON_CN_TITLE.fullmatch(t) or _PERSON_EN_TITLE.fullmatch(t):
        return True
    m = _PERSON_ORG_SUFFIX.search(t)
    if m:
        # 姓氏 Co 宽免（review P2）：菲华常见姓（Jonathan Co）。仅裸 "co"
        # （无缩写点）且其余 token 全部已知名才放行；"Acme Co" / "XX Co." 照杀。
        toks = t.split()
        if not (
            m.group(0).lower() == "co"
            and len(toks) >= 2
            and _is_all_known_name_tokens(toks[:-1])
        ):
            return True
    if _HAS_DIGIT.search(t):
        return True
    if _has_mixed_script_token(t):
        return True
    return False


def _is_all_known_name_tokens(toks: list[str]) -> bool:
    from .gazetteer import get_gazetteer

    gaz = get_gazetteer()
    return all(gaz.is_known_person_token(tok.rstrip(".")) for tok in toks)


# ── S2 salvage：junk span 内粘连真名的抢救（review P1 修订）──────────
# NER 对脏文本输出「张三、李四」「王小明(David)」「Rachel & Marcus」这类
# 粘连 span 是常态；整 span drop 会让其中的真名明文出墙，违反 fail-safe。
# 按句读符拆子段，**有姓名实证**的子段以子 span 保留；无实证子段（每次对话/
# 重新解释 这类句子碎片）不抢救——生产整句噪音的拦截力不回退。
def _has_person_name_evidence(t: str) -> bool:
    if _CJK_DOTTED_NAME.fullmatch(t):
        return True  # 迈克尔·乔丹 类间隔号译名
    if _CJK_NAME_2_4.fullmatch(t):
        # 中文姓名实证要求首字为常用姓氏——否则「非常烦」「重新解释」这类
        # 2-4 字句子碎片会被抢救成 PERSON（over-kill 治理目标回退）。
        return t[0] in _CN_SURNAMES or t.startswith(_CN_COMPOUND_SURNAMES)
    toks = t.split()
    if not 1 <= len(toks) <= _PERSON_MAX_WORDS:
        return False
    if not all(
        _LATIN_NAME_TOKEN.fullmatch(tok) or _CJK_SHORT_TOKEN.fullmatch(tok)
        for tok in toks
    ):
        return False
    from .gazetteer import get_gazetteer

    gaz = get_gazetteer()
    return any(gaz.is_known_person_token(tok.rstrip(".")) for tok in toks)


def _salvage_person_subspans(span: dict[str, Any]) -> list[dict[str, Any]]:
    text = span.get("text") or ""
    segments: list[tuple[int, int]] = []
    pos = 0
    for m in _PERSON_JUNK_CHARS.finditer(text):
        segments.append((pos, m.start()))
        pos = m.end()
    segments.append((pos, len(text)))
    out: list[dict[str, Any]] = []
    for seg_start, seg_end in segments:
        seg = text[seg_start:seg_end]
        stripped = seg.strip()
        if len(stripped) <= 1:
            continue
        if _is_person_junk(stripped) or not _has_person_name_evidence(stripped):
            continue
        lead = len(seg) - len(seg.lstrip())
        abs_start = span["start"] + seg_start + lead
        sub = dict(span)
        sub["start"] = abs_start
        sub["end"] = abs_start + len(stripped)
        sub["text"] = stripped
        out.append(sub)
    return out


# ── S7 单 token 英文 PERSON gate（负向名单）─────────────────────────
# 生产字典 85 条英文单名中约一半是产品/功能词（Obsidian/Vault/Income）。
# gate 只 drop「常用英文词（google-10000）且非名字且非拼音」的确定性误报；
# 未知罕见 token（Anouk/Hugues/Kek 等多元文化人名）**默认保留**——
# 首版正向 gate（不在名单即 drop）致 public_en PERSON recall 61→24 崩盘，
# 隐私语义下宁多脱敏不漏。名单外产品名由 suppression-list overlay 兜底。
# 多词全名与中文名不受此 gate 影响。
_SINGLE_LATIN_TOKEN = re.compile(r"^[A-Za-z][A-Za-z'\-]*$")


def _single_token_person_allowed(span: dict[str, Any]) -> bool:
    token = (span.get("text") or "").strip()
    if not _SINGLE_LATIN_TOKEN.fullmatch(token):
        return True  # 非单 token 拉丁词不归本 gate 管
    from .gazetteer import get_gazetteer

    return not get_gazetteer().is_common_nonname_word(token)


# ── S3 ADDRESS 粒度门控 ──────────────────────────────────────────────
# 生产字典 48 条 ADDRESS 中 32 条为粗粒度（纯数字/大洲/公共 POI），无个人定位能力。
_ADDR_PURE_NUM = re.compile(r"^[\d\s.\-#/,]*$")
# 结构要素：含行政/道路/门牌单位才认为具备可定位粒度。
_ADDR_STRUCT_CN = re.compile(r"[省市区县镇乡村街路巷弄号楼层室座栋幢里道]")
_ADDR_STRUCT_EN = re.compile(
    r"\b(?:street|st|road|rd|avenue|ave|boulevard|blvd|lane|ln|drive|dr"
    r"|block|building|floor|room|unit|suite|no)\b\.?",
    re.IGNORECASE,
)
_ADDR_SHORT_MAX = 6  # 无结构要素且 ≤6 字（北京南站/太清宫/香港）→ 公共地名，drop


def _is_address_junk(text: str) -> bool:
    t = text.strip()
    if not t or _ADDR_PURE_NUM.fullmatch(t):
        return True
    if len(t) <= _ADDR_SHORT_MAX and not (
        _ADDR_STRUCT_CN.search(t) or _ADDR_STRUCT_EN.search(t)
    ):
        return True
    return False


# ── S4 DOB 出生引导词 gate ───────────────────────────────────────────
# 生产 18 条 DOB 仅 2 条为真实生日，其余是会议/截止日期。统一要求窗口内有
# 出生语义引导词；cn_regex F11 的 lookbehind 引导词恰在窗口内，天然通过。
# 注意「生于」必须显式列（不能放宽到裸「生」——「2026年6月11日生效」会误触发）
_DOB_CONTEXT = re.compile(
    r"出生|生日|生于|诞生|诞辰|birth|born|\bdob\b|date of birth", re.IGNORECASE
)
_DOB_WINDOW_BEFORE = 30
_DOB_WINDOW_AFTER = 12  # 覆盖「1988年4月17日出生」后置写法
_DOB_YEAR = re.compile(r"(?:19|20)\d{2}")


def _dob_allowed(span: dict[str, Any], text: str, current_year: int) -> bool:
    m = _DOB_YEAR.search(span.get("text") or "")
    if m and int(m.group(0)) > current_year:  # 未来日期不可能是生日
        return False
    start, end = span["start"], span["end"]
    window = (
        text[max(0, start - _DOB_WINDOW_BEFORE):start]
        + text[end:end + _DOB_WINDOW_AFTER]
    )
    return bool(_DOB_CONTEXT.search(window))


# ── S6 SWIFT_BIC 双 gate ─────────────────────────────────────────────
# stdnum.bic 只验形态：任意 8/11 位大写英文词（生产实锤 ATTESTATION）可通过。
# gate ①：位置 5-6 必须是 ISO 3166 国家码（pycountry，公开数据源随 ISO 更新）；
# gate ②：窗口内必须有 SWIFT/汇款 语境词。真实码如 CMBCHKHHXXX 两关均过。
# 注意不收裸 \bbank\b——金融文本处处是 bank，「the bank requires ATTESTATION」
# 会把 S6 拦掉的形态误报重新放进来；beneficiary/iban 语境足够特异。
_BIC_CONTEXT = re.compile(
    r"swift|\bbic\b|汇款|电汇|收款行|收款银行|收款人|开户行|银行代码"
    r"|wire|remit|\biban\b|beneficiary|bank code",
    re.IGNORECASE,
)
_BIC_WINDOW = 60

try:
    import pycountry

    _ISO_ALPHA2: set[str] | None = {c.alpha_2 for c in pycountry.countries}
except ImportError:  # pragma: no cover - 依赖缺失时退化为仅上下文 gate
    logger.warning("pycountry 未安装，SWIFT_BIC 国家码校验跳过（仅上下文 gate）")
    _ISO_ALPHA2 = None


def _bic_allowed(span: dict[str, Any], text: str) -> bool:
    t = (span.get("text") or "").strip().upper()
    if _ISO_ALPHA2 is not None and t[4:6] not in _ISO_ALPHA2:
        return False
    start, end = span["start"], span["end"]
    window = (
        text[max(0, start - _BIC_WINDOW):start] + text[end:end + _BIC_WINDOW]
    )
    return bool(_BIC_CONTEXT.search(window))


# BIC 救援：HanLP 常把 BIC（DEUTDEFF）误标 ORG，优先级 3 > en_regex 1 抢走 span
# → BANK_CARD 丢失（e17-009 good case 实证）。BIC 形态 + 双 gate 通过的 ORG
# 改写回 BANK_CARD（与 F3b/F5 同族的 canonical 改写）。
_BIC_SHAPE = re.compile(r"^[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?$")


def _rescue_bic_mislabeled_org(
    span: dict[str, Any], text: str
) -> None:
    if span.get("canonical") != "ORG":
        return
    t = (span.get("text") or "").strip()
    if _BIC_SHAPE.fullmatch(t) and _bic_allowed(span, text):
        span["canonical"] = "BANK_CARD"
        span["raw_label"] = "SWIFT_BIC_RESCUED"


# ── 编排入口 ─────────────────────────────────────────────────────────
def apply_precision_rules(
    spans: list[dict[str, Any]],
    text: str,
    suppression: SuppressionList | None = None,
) -> list[dict[str, Any]]:
    """按 span 逐条应用 S 系列精度规则；suppression=None 时用全局单例（测试可注入）。"""
    sup = suppression if suppression is not None else get_suppression_list()
    current_year = date.today().year
    out: list[dict[str, Any]] = []
    for s in spans:
        canonical = s.get("canonical") or ""
        span_text = s.get("text") or ""
        if canonical == "PERSON" and _is_person_junk(span_text):
            # 不整 span drop：先抢救粘连子段（张三、李四 → 两个子 span），
            # 抢救结果继续过 S7/S1；无可抢救子段 = 维持 drop。
            candidates = _salvage_person_subspans(s)
        else:
            candidates = [s]
        for c in candidates:
            c_text = c.get("text") or ""
            if canonical == "PERSON" and not _single_token_person_allowed(c):
                continue
            if canonical == "ADDRESS" and _is_address_junk(c_text):
                continue
            if canonical == "DOB" and not _dob_allowed(c, text, current_year):
                continue
            if c.get("raw_label") == "SWIFT_BIC" and not _bic_allowed(c, text):
                continue
            _rescue_bic_mislabeled_org(c, text)
            if sup.hit(c.get("canonical") or "", c_text):
                continue
            out.append(c)
    return out
