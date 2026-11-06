"""Split-based span merge.

优先级：cn_regex > hanlp > nvidia > en_regex
详见 docs/features/redaction/2026-05-19-pii-redaction-implementation.md §1.7
port 自 privacy-filter-eval/reports/nvda_case/run_pipeline.py:merge_spans
"""
from __future__ import annotations

import re
from typing import Any

from .detectors.cn_regex import ADDR_SUFFIX_UNIT  # 门牌后缀文法单一事实源（含"号"歧义守护）
from .precision import apply_precision_rules

# urchade 与其他 GLiNER 候选承担与 nvidia 相同的 NER 角色，同优先级 2（A/B 时不共存）
# spacy/presidio 同为英文 NER 角色（非 GLiNER 轻量方案），同优先级 2。
PRIORITY: dict[str, int] = {
    "cn_regex": 4, "hanlp": 3,
    "nvidia": 2, "urchade": 2, "knowledgator": 2,  # GLiNER 候选同 NER 角色优先级
    "spacy": 2, "presidio": 2,                      # 非 GLiNER 轻量 NER 候选
    "en_regex": 1,
    "gazetteer": 1,  # S7 人名补召回：纯补漏，与 NER 重叠时 NER 胜出
}

MERGE_DROP_CARVED_RATIO = 0.30
MERGE_MIN_LEFTOVER_CHARS = 3

# F5: USCC（统一社会信用代码）字面与 18 位身份证 / 银行卡区分
# 大陆 ID_CARD 的 6 位区划码必然以 11-65 起头（省级编码），
# 不可能以 "9" 起头；而 USCC 第一段（登记管理部门）常用 9（工商）。
# span text 中含以 9 开头的 18 位字母数字 → 强制改 canonical=BUSINESS_ID
# 用 re.search 是因为 NVIDIA 经常把 "USCC " 前缀一起吃进 span
_USCC_PATTERN = re.compile(r"(?<![A-Za-z0-9])9[0-9A-HJ-NPQRTUWXY]{17}(?![A-Za-z0-9])")

# F3b: NVIDIA 经常把 crypto 地址标成 BANK_CARD / national id 抢走 en_regex 命中。
# span text 中含以下任一加密钱包格式 → 强制 canonical=CRYPTO_ADDR
_CRYPTO_ADDR_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:bc1[0-9a-z]{25,90}"          # BTC bech32
    r"|[13][a-km-zA-HJ-NP-Z1-9]{25,34}"  # BTC P2PKH/P2SH
    r"|0x[a-fA-F0-9]{40}"             # ETH
    r"|T[1-9A-HJ-NP-Za-km-z]{33})"    # TRX
    r"(?![A-Za-z0-9])"
)

# F6 post: NVIDIA 把 20+ 位的对公账号标成 BANK_CARD，按位数改 ACCOUNT
_LONG_DIGIT_PATTERN = re.compile(r"(?<!\d)\d{20,25}(?!\d)")

# F7: ORG_PUBLIC 白名单已泛化为 S1 通用值抑制名单（round11），
# 迁至 config/suppression-list.yaml（ORG 段，语义不变），
# 由 apply_context_rules 尾部的 precision.apply_precision_rules 统一应用。


def _digit_count(text: str) -> int:
    return sum(1 for ch in text if ch.isdigit())


def _post_process_uscc(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for s in spans:
        if s.get("canonical") not in {"ID_CARD", "BANK_CARD"}:
            continue
        text = s.get("text") or ""
        if _USCC_PATTERN.search(text):
            s["canonical"] = "BUSINESS_ID"
            s["raw_label"] = "USCC"
    return spans


def _post_process_crypto(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for s in spans:
        if s.get("canonical") not in {"BANK_CARD", "ID_CARD", "ACCOUNT"}:
            continue
        text = s.get("text") or ""
        if _CRYPTO_ADDR_PATTERN.search(text):
            s["canonical"] = "CRYPTO_ADDR"
            s["raw_label"] = "CRYPTO_ADDR"
    return spans


# F9: CRYPTO_ADDR 假阳性过滤
# 拒识：① 全 0 / 全 f / 同字符 6+ run 的 ETH；② 同字符 6+ run 的 BTC；
#       ③ 纯数字 ETH（无 a-f 字母，0x 后 40 hex 全 0-9，真实链上罕见）
def _has_long_identical_run(s: str, n: int = 6) -> bool:
    for i in range(len(s) - n + 1):
        if len(set(s[i:i + n])) == 1:
            return True
    return False


def _is_likely_crypto_junk(text: str) -> bool:
    """text 已确认形态像 crypto 地址；判断是否明显是垃圾/陷阱串。"""
    if text.startswith("0x") and len(text) == 42:
        body = text[2:]
        if _has_long_identical_run(body, 6):
            return True
        # 全部 0-9 无 a-f/A-F → 真实 ETH 地址几乎不可能
        if not any(c.lower() in "abcdef" for c in body):
            return True
        return False
    # BTC bech32 / legacy
    body = text[3:] if text.startswith("bc1") else text[1:]
    return _has_long_identical_run(body, 6)


def _post_process_crypto_junk(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """F9: drop 明显的假 crypto 地址。"""
    return [
        s for s in spans
        if not (
            s.get("canonical") == "CRYPTO_ADDR"
            and _is_likely_crypto_junk(s.get("text") or "")
        )
    ]


# F8: span 前 30 字符内含 \bTIN\b 关键字 → ID_CARD/BUSINESS_ID/ACCOUNT 改 TIN
# 解决 HK/SG/EIN 在 CRS/FATCA 上下文里应当被识别为 TIN 而非 ID_CARD/BUSINESS_ID 的问题
_TIN_KEYWORD = re.compile(r"\bTIN\b", re.IGNORECASE)
_TIN_CONTEXT_WINDOW = 30


# F10b 相邻 ADDRESS 桥接：NER（HanLP/gliner）常把一个中文地址切成多段
# （"浦东" / "世纪大道" …），中间的连接字（区/号/数字/空格等）漏在 span 外。
# 两个 ADDRESS span 间隔 ≤4 字且 gap 全为地址连接字 → 缝合为一个完整 span。
# 注：cn_regex 的结构化 CN_ADDRESS（优先级最高）已 carve 掉多数碎片，本步是兜底收尾。
_ADDR_CONNECTOR = set("省市区县镇乡街道路号弄室楼层座栋幢单元巷弄里")
_ADDR_BRIDGE_MAX_GAP = 4

# F10c 地址尾部补全：CN_ADDRESS 的门牌后缀文法（cn_regex `\d+楼/层/室…`）在中间夹了
# 楼宇名（"…100 号上海中心大厦 56 楼 5601 室"）时会被打断，桥接也只缝合已有 ADDRESS span、
# 不向右扩展，导致「楼 5601 室」尾巴丢失。本步对每个 ADDRESS span 向右贪心吸收紧邻的
# 号段，文法复用 cn_regex.ADDR_SUFFIX_UNIT（单一事实源，含"号"歧义守护）。
# 只吃以单位字结尾的号段 → 电话（11 位连号、无单位字结尾）、纯长数字串吸不进，FP 安全。
_ADDR_TAIL_RE = re.compile(rf"(?:{ADDR_SUFFIX_UNIT})+")


def _extend_address_tail(
    spans: list[dict[str, Any]], text: str
) -> list[dict[str, Any]]:
    for s in spans:
        if s.get("canonical") != "ADDRESS":
            continue
        m = _ADDR_TAIL_RE.match(text, s["end"])
        if m and m.end() > s["end"]:
            s["end"] = m.end()
            s["text"] = text[s["start"]:m.end()]
            s["raw_label"] = "ADDRESS_BRIDGED"
    return spans


# F10d 地址前导裁剪：NER（HanLP/gliner）有时把地址前的引导词并进 ADDRESS span 起点
# （动词"住/现住/位于"、标签"地址：/住址："…）。例：原文"现住上海…"，NER 丢"现"却把
# "住"留在 span 起点 → 脱敏值/还原显示夹无关字。这些引导词非地理实体，应剥离。与 F10c
# 尾部补全对称（一头一尾）。FP 守护：① 只裁固定引导词表（不含 bare"居/市/路"等地名字，
# 长词优先、bare"住"垫底）；② 其后必须紧跟地理行政字（lookahead）才裁，挡"居安路"
# "位于公司内部"等非地址前缀的误裁。
_ADDR_HEAD_RE = re.compile(
    r"^(?:现|家|长期|暂)?"
    r"(?:现居住|现居|居住|定居|户籍地|户籍|住所地|住所|住址|地址|位于|坐落于|坐落|住)"
    r"[在于]?[:：，,\s]*"
    r"(?=[一-龥]{0,4}"
    r"(?:省|市|区|县|自治区|自治州|盟|旗|镇|乡|村|街道|路|街|大道|大街|道|巷|弄|号))"
)


def _trim_address_head(
    spans: list[dict[str, Any]], text: str
) -> list[dict[str, Any]]:
    for s in spans:
        if s.get("canonical") != "ADDRESS":
            continue
        span_text = text[s["start"]:s["end"]]
        m = _ADDR_HEAD_RE.match(span_text)
        if m and 0 < m.end() < len(span_text):
            s["start"] += m.end()
            s["text"] = text[s["start"]:s["end"]]
            s["raw_label"] = "ADDRESS_HEAD_TRIMMED"
    return spans


def _bridge_address_spans(
    spans: list[dict[str, Any]], text: str
) -> list[dict[str, Any]]:
    addr = sorted(
        (s for s in spans if s.get("canonical") == "ADDRESS"),
        key=lambda s: s["start"],
    )
    if len(addr) < 2:
        return spans
    others = [s for s in spans if s.get("canonical") != "ADDRESS"]
    merged: list[dict[str, Any]] = []
    cur = addr[0]
    for nxt in addr[1:]:
        gap_len = nxt["start"] - cur["end"]
        gap = text[cur["end"]:nxt["start"]] if gap_len > 0 else ""
        bridgeable = 0 <= gap_len <= _ADDR_BRIDGE_MAX_GAP and all(
            c.isspace() or c.isdigit() or c in _ADDR_CONNECTOR for c in gap
        )
        if bridgeable:
            cur = {
                **cur,
                "end": nxt["end"],
                "text": text[cur["start"]:nxt["end"]],
                "raw_label": "ADDRESS_BRIDGED",
            }
        else:
            merged.append(cur)
            cur = nxt
    merged.append(cur)
    return sorted(others + merged, key=lambda s: s["start"])


def apply_context_rules(
    spans: list[dict[str, Any]], text: str
) -> list[dict[str, Any]]:
    """需要访问原文的上下文相关规则，merge 后调用。"""
    for s in spans:
        canon = s.get("canonical")
        if canon not in {"ID_CARD", "BUSINESS_ID", "ACCOUNT"}:
            continue
        start = s.get("start", 0)
        window = text[max(0, start - _TIN_CONTEXT_WINDOW):start]
        if _TIN_KEYWORD.search(window):
            s["canonical"] = "TIN"
            s["raw_label"] = "TIN_CONTEXT"
    # 顺序：先桥接碎片 → 尾部补全 → 对完整 ADDRESS span 做前导裁剪（一头一尾对称）
    # → 最后跑 S 系列精度规则（round11：抑制名单 + 垃圾 span + DOB/BIC gate），
    #   须在地址整形之后（桥接后的完整地址才有结构要素，避免误杀碎片期形态）。
    shaped = _trim_address_head(
        _extend_address_tail(_bridge_address_spans(spans, text), text), text
    )
    return apply_precision_rules(shaped, text)


def _post_process_account(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """BANK_CARD 但去空格/dash 后 20+ 位 → 改 ACCOUNT。
    标准 BANK_CARD（信用卡 16 / 借记卡 19）位数应 ≤19。"""
    for s in spans:
        if s.get("canonical") != "BANK_CARD":
            continue
        text = s.get("text") or ""
        # 纯数字串或带空格/dash 分组的数字串
        if _LONG_DIGIT_PATTERN.search(text):
            s["canonical"] = "ACCOUNT"
            s["raw_label"] = "ACCOUNT_LONG"
            continue
        stripped = text.replace(" ", "").replace("-", "")
        if stripped.isdigit() and len(stripped) >= 20:
            s["canonical"] = "ACCOUNT"
            s["raw_label"] = "ACCOUNT_LONG"
    return spans


def _carve_segments(
    free: list[tuple[int, int]],
    occupant_start: int,
    occupant_end: int,
) -> list[tuple[int, int]]:
    """从 free 段列表里挖掉 [occupant_start, occupant_end) 区间"""
    out: list[tuple[int, int]] = []
    for fs, fe in free:
        if occupant_end <= fs or occupant_start >= fe:
            out.append((fs, fe))
            continue
        if fs < occupant_start:
            out.append((fs, occupant_start))
        if occupant_end < fe:
            out.append((occupant_end, fe))
    return out


def merge_spans(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """分裂式合并：
    - 按 (priority DESC, score DESC, length DESC) 排序
    - 高优先级先处理；低优先级被部分覆盖时按 carved_ratio 决定保留 leftover
    """
    if not spans:
        return []
    sorted_spans = sorted(
        spans,
        key=lambda s: (
            -PRIORITY.get(s["source"], 0),
            -float(s["score"]),
            -(s["end"] - s["start"]),
        ),
    )

    merged: list[dict[str, Any]] = []

    for s in sorted_spans:
        original_len = s["end"] - s["start"]
        if original_len <= 0:
            continue
        free: list[tuple[int, int]] = [(s["start"], s["end"])]
        for m in merged:
            free = _carve_segments(free, m["start"], m["end"])
            if not free:
                break
        if not free:
            continue

        leftover_len = sum(fe - fs for fs, fe in free)
        if leftover_len == original_len:
            merged.append(s)
            continue

        carved_ratio = 1.0 - (leftover_len / original_len)
        if carved_ratio >= MERGE_DROP_CARVED_RATIO:
            continue

        for fs, fe in free:
            seg_len = fe - fs
            if seg_len < MERGE_MIN_LEFTOVER_CHARS:
                continue
            offset_in_orig = fs - s["start"]
            merged.append({
                **s,
                "start": fs,
                "end": fe,
                "text": s["text"][offset_in_orig:offset_in_orig + seg_len],
                "split_from_original": True,
            })

    # 顺序敏感：crypto 先于 account（避免长 hex 串被 account 抢）。
    # 原 F7 ORG_PUBLIC drop 已迁入 apply_context_rules 尾部的 S1 抑制名单（round11）。
    merged = _post_process_crypto(merged)
    merged = _post_process_uscc(merged)
    merged = _post_process_account(merged)
    merged = _post_process_crypto_junk(merged)
    return sorted(merged, key=lambda s: s["start"])
