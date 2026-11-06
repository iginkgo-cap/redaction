"""CN 正则识别器：核心（手机/座机/邮箱/身份证/银行卡）+ 家办业务标识。

身份证 + 银行卡带轻度 validator（位数 / Luhn）。
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from stdnum.luhn import is_valid as luhn_is_valid

from .base import Detector


# F2 BUSINESS_ID 前缀清单：家办金融场景常见业务私有标识
# - 客户号 CST, 合同号 HT, LP 协议 LP, 订单 ORD, 交易 TRD, IC 纪要 IC,
#   主协议 MSA, 三方 TRI, 终止 TERM, 保密 NDA, 基金份额 FA, 项目 PRJ,
#   KYC 编号 KYC, HK 公司号 CR, Cayman 公司号 CY, Jersey 信托号 TR,
#   采购 PO, 子顾问 SUB
_BUSINESS_ID_PREFIX = (
    r"(?:CST|HT|LP|ORD|TRD|IC|MSA|TRI|TERM|NDA|FA|PRJ|KYC|CR|CY|TR|PO|SUB)"
)

# F2b USCC（统一社会信用代码）主检测：18 位 = 2 字符 + 6 数字 + 10 字符，
# 字符集排除易混字母 I/O/S/V/Z（GB 32100）。validator 要求前 17 位至少含 1 字母，
# 借此避开 18 位身份证 / 银行卡的形态碰撞；纯数字 USCC 仍由
# merge._post_process_uscc（leading-9）兜底改写，二者互补。
_USCC_RE = re.compile(
    r"(?<![A-Za-z0-9])[0-9A-HJ-NP-RTUW-Y]{2}\d{6}[0-9A-HJ-NP-RTUW-Y]{10}(?![A-Za-z0-9])"
)

# 门牌后缀单元（楼/层/室/单元/座/栋 + 受守护的"号"）。
# merge._extend_address_tail 复用同一文法（单一事实源），改这里两处同时生效。
# 尾部"N 号"歧义守护：门牌主号之后的"号"若紧跟非地址名词（8 号线 / 3 号文件 /
# N 号公告 / N 号台风 …）则不计入地址，避免把线路/文号/公告等吸进 span；
# 而"N 号楼""房号 N 号。"等合法地址尾段仍保留（后随地址单位或句末/标点不被挡）。
_ADDR_NONADDR_AFTER_HAO = r"(?:线|文件|公告|通知|令|台风)"
ADDR_SUFFIX_UNIT = (
    r"\s*\d{0,4}\s*"
    r"(?:室|楼|层|单元|座|栋|号(?!\s*" + _ADDR_NONADDR_AFTER_HAO + r"))"
)

# F10 中文地址结构正则（省?市?区? + 道路 + 门牌号），整段命中以避免 NER 碎片化。
# 行政段长度收紧（市≤3 / 区≤4 / 路核≤5）以减少把前缀动词吃进 span；
# 门牌数字后必须跟 号/室/栋 等单位标记，降低对"丝绸之路 2000 年"类的误命中。
# 注：可能粘连 1 个前导字（如"…在|上海市…"中的"在"），对脱敏无害——
# restore 按 span 原文精确放回，不泄漏，仅占位符边界早 1 字。
_CN_ADDRESS_RE = re.compile(
    r"(?:[一-龥]{2,4}(?:省|自治区|特别行政区))?"
    r"(?:[一-龥]{2,3}(?:市|自治州|地区|盟))?"
    r"(?:[一-龥]{2,4}(?:区|县|旗))?"
    r"[一-龥]{1,5}?(?:路|街|大街|大道|道|巷|弄|胡同)"
    r"\s*\d{1,5}\s*(?:号|弄|室|栋|幢|座)"
    rf"(?:{ADDR_SUFFIX_UNIT})*"
)

# 手机号 3-4-4 分组（138 0176 5924 / 139-1728-4410）；前后不能挨着别的数字组，免得吃进分组卡号
_CN_PHONE_GROUPED_RE = re.compile(
    r"(?<!\d)(?<!\d[ \u00a0-])1[3-9]\d[ \u00a0-]\d{4}[ \u00a0-]\d{4}(?![ \u00a0-]?\d)"
)

# 座机：区号 010/02X 或 0[3-9]XX，与号码之间必须有分隔或括号（连续 11 位数字太像编号）；
# 号码 7-8 位且首位 2-9，可带 -801 / 转 801 分机；前后挨着别的数字组（分组 IBAN/卡号）不算
_CN_LANDLINE_RE = re.compile(
    r"(?<![\d-])(?<![A-Za-z\d][ \u00a0])"
    r"(?:[(（]0(?:10|2\d|[3-9]\d{2})[)）]\s?|0(?:10|2\d|[3-9]\d{2})[ \u00a0-])"
    r"[2-9]\d{2,3}[ \u00a0-]?\d{4}"
    r"(?:(?:-|\s?(?:转|分机|ext\.?)\s?)\d{1,5})?"
    r"(?![\d-])(?![ \u00a0]\d)",
    re.IGNORECASE,
)

_PATTERNS = [
    ("CN_PHONE", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), 0.95, None),
    ("CN_PHONE", _CN_PHONE_GROUPED_RE, 0.95, None),
    ("CN_LANDLINE", _CN_LANDLINE_RE, 0.9, None),
    ("CN_EMAIL", re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?!\.?[A-Za-z0-9])"), 0.99, None),
    ("CN_ID_CARD", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), 0.9, "id_card"),
    ("CN_BANK_CARD", re.compile(r"(?<!\d)\d{13,19}(?!\d)"), 0.65, "luhn"),
    (
        "CN_BUSINESS_ID",
        re.compile(rf"(?<![A-Za-z0-9]){_BUSINESS_ID_PREFIX}-[A-Z0-9]+(?:-[A-Z0-9]+)*(?![A-Za-z0-9-])"),
        0.95,
        None,
    ),
    # F2b USCC 统一社会信用代码（主检测，补 merge 改写器覆盖不到的字母型 USCC）
    ("CN_USCC", _USCC_RE, 0.9, "uscc"),
    # F10 中文结构化地址（整段命中，cn_regex 优先级最高会 carve 掉 NER 碎片）
    ("CN_ADDRESS", _CN_ADDRESS_RE, 0.8, None),
    # F6 ACCOUNT 类（家办金融特有）
    # 港式对公账号：3-6-3 数字（如 808-123456-001 HSBC HK）
    (
        "CN_ACCOUNT_HK",
        re.compile(r"(?<![\d-])\d{3}-\d{6}-\d{1,3}(?![\d-])"),
        0.9,
        None,
    ),
    # 新加坡 / 海外 dash 账号：0-XXX-XXXXXX-X 等
    (
        "CN_ACCOUNT_SG",
        re.compile(r"(?<![\d-])\d{1,3}-\d{1,3}-\d{6}-\d{1,3}(?![\d-])"),
        0.85,
        None,
    ),
    # 国内对公账号：20-25 位连续数字（超出 BANK_CARD Luhn 范围）
    (
        "CN_ACCOUNT_LONG",
        re.compile(r"(?<!\d)\d{20,25}(?!\d)"),
        0.88,
        None,
    ),
    # 中登券商投资人代码：A + 10 位数字
    (
        "CN_INVESTOR_CODE",
        re.compile(r"(?<![A-Za-z0-9])A\d{10}(?![0-9])"),
        0.85,
        None,
    ),
    # T12 DOB 中文：只覆盖明显的"1985 年 5 月 12 日"格式
    # （纯 YYYY-MM-DD 太宽，会误命中合同日期 / 事件日期等，留给上下文/NER 兜底）
    (
        "CN_DOB",
        re.compile(
            r"(?<!\d)(?:19|20)\d{2}\s?年\s?\d{1,2}\s?月\s?\d{1,2}\s?日(?!\d)"
        ),
        0.9,
        None,
    ),
    # F11 DOB ISO（YYYY-MM-DD）：仅在出生引导词后命中，避免把事件/合同日期误判为出生日期
    # （延续上一条注释的设计意图）。定宽 lookbehind 只截日期、不含引导词，引导词各一条。
    (
        "CN_DOB",
        re.compile(r"(?<=出生日期[:：])(?:19|20)\d{2}-\d{1,2}-\d{1,2}(?!\d)"),
        0.9,
        None,
    ),
    (
        "CN_DOB",
        re.compile(r"(?<=出生[:：])(?:19|20)\d{2}-\d{1,2}-\d{1,2}(?!\d)"),
        0.9,
        None,
    ),
    (
        "CN_DOB",
        re.compile(r"(?<=生日[:：])(?:19|20)\d{2}-\d{1,2}-\d{1,2}(?!\d)"),
        0.9,
        None,
    ),
    # T12 TIN 美国 SSN: 123-45-6789
    (
        "CN_TIN_US_SSN",
        re.compile(r"(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])"),
        0.85,
        None,
    ),
    # F8 EIN: 美国雇主税号 12-3456789（默认归 BUSINESS_ID；
    # 若上下文有 TIN 关键字会在 main 端被 context rule 提升到 TIN）
    (
        "CN_BUSINESS_ID_EIN",
        re.compile(r"(?<![\d-])\d{2}-\d{7}(?![\d-])"),
        0.8,
        None,
    ),
    # T12 TIN 香港 8 位（A-Z + 7 digits + 校验位）  ↔ 与香港身份证形态相同，
    # 仅在显式 TIN 标签上下文里生效；纯正则区分不开，留给 NVIDIA 兜底
    # （此处仅声明 raw_label，不实际命中，避免 FP）
]


def id_card_plausible(text: str) -> bool:
    """18 位身份证形态校验（LOOK-6827）：首位非 0 + 出生日期合法 + 末位数字或 X。

    首位放到 1–9：居民身份证 1–6，港澳台居民居住证 81/82/83，2023 版外国人永居证 9 开头，出生日期都在 7–14 位。

    刻意**不校验 GB 11643 校验位**：输错一位的真身份证仍应打码；而 0 开头的 18 位业务 ID、
    时间戳类数字因首位为 0 或日期非法被排除（生产字典里这类误报占工具输出 ID_CARD 的绝大多数）。
    """
    if len(text) != 18 or not text[:17].isdigit():
        return False
    if not (text[-1] in "Xx" or text[-1].isdigit()):
        return False
    if text[0] == "0":
        return False
    try:
        born = date(int(text[6:10]), int(text[10:12]), int(text[12:14]))
    except ValueError:
        return False
    return date(1900, 1, 1) <= born <= date.today()


def _validate(text: str, kind: str) -> bool:
    if kind == "luhn":
        return luhn_is_valid(text)
    if kind == "id_card":
        return id_card_plausible(text)
    if kind == "uscc":
        # 前 17 位至少含 1 字母 → 避开 18 位纯数字身份证 / 银行卡碰撞
        # （纯数字 USCC 由 merge._post_process_uscc 兜底改写）
        return any(c.isalpha() for c in text[:17])
    return True


class CnRegexDetector(Detector):
    name = "cn_regex"

    async def detect(self, text: str) -> list[dict[str, Any]]:
        spans: list[dict[str, Any]] = []
        for raw_label, pattern, score, validator in _PATTERNS:
            canonical = self.canonical_map.lookup("cn_regex", raw_label)
            if canonical is None:
                continue
            for m in pattern.finditer(text):
                t = m.group(0)
                if validator and not _validate(t, validator):
                    continue
                spans.append({
                    "source": "cn_regex",
                    "raw_label": raw_label,
                    "canonical": canonical,
                    "start": m.start(),
                    "end": m.end(),
                    "text": t,
                    "score": score,
                })
        return spans
