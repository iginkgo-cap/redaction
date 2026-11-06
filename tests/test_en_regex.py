"""en_regex.py 单测：8 条家办金融正则 + PHONE_INTL。

每条规则覆盖：positive（应命中）+ negative（不应命中）+ validator 行为
"""
from __future__ import annotations

import pytest

from app.detectors.en_regex import EnRegexDetector


@pytest.fixture
def detector(canonical_map, en_rules_path):
    return EnRegexDetector(canonical_map, en_rules_path)


async def _detect(detector, text: str) -> list[dict]:
    return await detector.detect(text)


def _hits(spans: list[dict], canonical: str) -> list[str]:
    return [s["text"] for s in spans if s["canonical"] == canonical]


async def test_email(detector) -> None:
    s = await _detect(detector, "contact john.smith+work@example.co.uk for details")
    assert _hits(s, "EMAIL") == ["john.smith+work@example.co.uk"]


# S5 (round11)：全量 URL 识别下线（生产 84/84 为公开网站 0 敏感），
# 仅保留敏感特征 URL 三条（userinfo / 凭据 query / 内网 host）。
async def test_url_公开网站_不再识别(detector) -> None:
    s = await _detect(detector, "see https://example.com/path?q=1 and http://x.org")
    assert _hits(s, "URL") == []


async def test_url_userinfo凭据_识别(detector) -> None:
    s = await _detect(detector, "db at https://admin:s3cret@db.internal.example.com/prod")
    urls = _hits(s, "URL")
    assert urls == ["https://admin:s3cret@db.internal.example.com/prod"]


async def test_url_query凭据参数_识别(detector) -> None:
    s = await _detect(detector, "link: https://api.example.com/v1/export?access_token=abc123def")
    urls = _hits(s, "URL")
    assert urls == ["https://api.example.com/v1/export?access_token=abc123def"]


async def test_url_内网host_识别(detector) -> None:
    s = await _detect(detector, "服务在 http://192.168.1.42:8080/admin 上")
    urls = _hits(s, "URL")
    assert "http://192.168.1.42:8080/admin" in urls


async def test_url_github公开链接_不识别(detector) -> None:
    # 生产字典最高频误报（github.com 18 条）
    s = await _detect(detector, "参考 https://github.com/openai/whisper/blob/main/README.md")
    assert _hits(s, "URL") == []


async def test_ipv4_公网IP命中(detector) -> None:
    """D11 回归 IP 识别（P7 加固正则）：公网 IP 应命中"""
    s = await _detect(detector, "Server logs show access from 8.8.8.8 yesterday.")
    assert _hits(s, "IP_ADDRESS") == ["8.8.8.8"]


async def test_ipv4_内网IP命中(detector) -> None:
    # S06 用例场景：内网 IP 也识别（语义取舍由前端逐项开关兜底，见 D11）
    s = await _detect(detector, "Connection from 192.168.1.42 logged.")
    assert _hits(s, "IP_ADDRESS") == ["192.168.1.42"]


async def test_ipv4_句尾句号不影响命中(detector) -> None:
    # P7 的 (?!\.?\d) 只挡「点+数字」延续，句尾句号应放行（P1 草稿曾误杀）
    s = await _detect(detector, "The request originated from 198.51.100.155.")
    assert _hits(s, "IP_ADDRESS") == ["198.51.100.155"]


async def test_ipv4_中文风控语境命中(detector) -> None:
    s = await _detect(detector, "用户登录 IP 为 114.114.114.114，与常用地不符。")
    assert _hits(s, "IP_ADDRESS") == ["114.114.114.114"]


async def test_ipv4_带端口命中IP部分(detector) -> None:
    s = await _detect(detector, "transfer initiated from 91.198.174.192:443")
    assert _hits(s, "IP_ADDRESS") == ["91.198.174.192"]


async def test_ipv4_D8五类FP场景全拒绝(detector) -> None:
    """D8 评测列举的 5 类高频误报，P7 必须全部不命中（D11 回归的前提条件）"""
    negatives = [
        "备份生成于 2025.10.05.18.30.22 凌晨",            # 时间戳
        "restore backup-2025.10.05.18.30.22.tar.gz",      # 时间戳文件名
        "网络拓扑：192.168.0.0/24 为办公网段",             # CIDR 网段（拓扑描述非 PII）
        "service v7.0.0.1 released to prod",              # 版本号 v 前缀
        "调用 api.example.com:8080 接口",                  # 域名端口
    ]
    for n in negatives:
        s = await _detect(detector, n)
        assert _hits(s, "IP_ADDRESS") == [], f"误识别 {n}: {s}"


async def test_ipv4_版本号类FP拒绝(detector) -> None:
    # D8 之外的补充负样本（实验 P7 验证项）
    negatives = [
        "Kernel 5.15.0.91-generic on host",     # 内核版本（后缀 -generic）
        "报告基准日 2026.05.10 收盘价",           # 点分日期
        "基金净值 1.0540，七日年化 3.2%",         # 净值小数
        "call +1.212.555.0182 now",             # 点分电话
        "Chrome/120.0.6099.71 Safari/537.36",   # UA 版本串
    ]
    for n in negatives:
        s = await _detect(detector, n)
        assert _hits(s, "IP_ADDRESS") == [], f"误识别 {n}: {s}"


async def test_iban_validator(detector) -> None:
    # GB82WEST12345698765432 是已知有效 IBAN（Presidio 示例）
    s = await _detect(detector, "IBAN GB82WEST12345698765432 confirmed")
    assert "GB82WEST12345698765432" in _hits(s, "BANK_CARD")


async def test_iban_validator_拒绝无效(detector) -> None:
    # 改一位校验，应被 stdnum.iban.is_valid 拒绝
    s = await _detect(detector, "IBAN GB99WEST12345698765432 fake")
    assert _hits(s, "BANK_CARD") == []


async def test_credit_card_luhn(detector) -> None:
    # 4012888888881881 是 Luhn 有效（测试卡号）
    s = await _detect(detector, "card 4012888888881881")
    assert _hits(s, "BANK_CARD") == ["4012888888881881"]


async def test_credit_card_luhn_拒绝无效(detector) -> None:
    # 改一位
    s = await _detect(detector, "card 4012888888881882")
    assert _hits(s, "BANK_CARD") == []


async def test_银联借记卡_19位连续(detector) -> None:
    # 6222021234567890128：银联 62 开头 19 位借记卡，Luhn 有效（合成测试号，非真人卡）
    # 国内最常见卡号长度，此前测试只覆盖 16 位 Visa，补 19 位边界
    s = await _detect(detector, "卡号 6222021234567890128 开户工行")
    assert _hits(s, "BANK_CARD") == ["6222021234567890128"]


async def test_银联借记卡_19位带空格(detector) -> None:
    # 真实输入/复制常带 4 位空格分组；validator 前 cleaned 去分隔符再 Luhn 校验
    s = await _detect(detector, "卡号 6222 0212 3456 7890 128")
    assert _hits(s, "BANK_CARD") == ["6222 0212 3456 7890 128"]


async def test_银联借记卡_19位带横线(detector) -> None:
    s = await _detect(detector, "卡号 6222-0212-3456-7890-128")
    assert _hits(s, "BANK_CARD") == ["6222-0212-3456-7890-128"]


async def test_银联借记卡_19位Luhn无效拒绝(detector) -> None:
    # 6222021234567890123：末位非合法校验位，Luhn 无效（即此前 e2e 误用、被正确拒绝的卡号）
    s = await _detect(detector, "卡号 6222021234567890123")
    assert _hits(s, "BANK_CARD") == []


async def test_crypto_btc(detector) -> None:
    s = await _detect(detector, "BTC 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa is famous")
    assert _hits(s, "CRYPTO_ADDR") == ["1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"]


async def test_crypto_eth(detector) -> None:
    s = await _detect(detector, "ETH 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb7")
    assert _hits(s, "CRYPTO_ADDR") == ["0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb7"]


async def test_phone_intl_格式覆盖(detector) -> None:
    cases = [
        "+1-212-555-0182",
        "+1 212 555 0182",
        "+86 138 0013 8000",
        "+44 (0)20 7946 0958",
        "+852 2581 1234",
    ]
    for case in cases:
        s = await _detect(detector, f"call {case} now")
        phones = _hits(s, "PHONE")
        assert len(phones) == 1, f"{case} 未命中：{phones}"


async def test_phone_intl_负面(detector) -> None:
    negatives = [
        "13800138000",                     # 无 + 不命中
        "GB82WEST12345698765432",          # IBAN
        "version +1.2.3",                  # 版本号
        "https://example.com/+1path",      # URL 里 +1
    ]
    for n in negatives:
        s = await _detect(detector, n)
        assert _hits(s, "PHONE") == [], f"误识别 {n}: {s}"


async def test_passport_generic(detector) -> None:
    s = await _detect(detector, "passport A1234567")
    assert _hits(s, "ID_CARD") == ["A1234567"]


async def test_passport_双字母前缀(detector) -> None:
    # 中国电子护照 EA/EB/EJ… 双字母系列：旧正则只认单字母会漏
    s = await _detect(detector, "护照号 EJ9273612 已登记")
    assert "EJ9273612" in _hits(s, "ID_CARD")


async def test_passport_尾字母(detector) -> None:
    # 新加坡护照：字母 + 7 数字 + 尾校验字母
    s = await _detect(detector, "passport K1234567X issued")
    assert "K1234567X" in _hits(s, "ID_CARD")


async def test_swift_bic_validator(detector) -> None:
    # DEUTDEFF 是德意志银行有效 BIC
    s = await _detect(detector, "SWIFT DEUTDEFF for payment")
    assert "DEUTDEFF" in _hits(s, "BANK_CARD")


async def test_swift_bic_validator_拒绝(detector) -> None:
    # ZZZZZZZZ 国家码非法
    s = await _detect(detector, "SWIFT ZZZZZZZZ fake")
    assert "ZZZZZZZZ" not in _hits(s, "BANK_CARD")


async def test_source_与_raw_label(detector) -> None:
    s = await _detect(detector, "a@b.com")
    assert s[0]["source"] == "en_regex"
    assert s[0]["raw_label"] == "EMAIL"
    assert s[0]["canonical"] == "EMAIL"


async def test_text_切片与_start_end_一致(detector) -> None:
    text = "call +1-212-555-0182 or email a@b.com"
    spans = await _detect(detector, text)
    for s in spans:
        assert text[s["start"]:s["end"]] == s["text"]


async def test_F3a_crypto_BTC_bech32(detector) -> None:
    spans = await _detect(detector, "BTC: bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh")
    addrs = [s for s in spans if s["canonical"] == "CRYPTO_ADDR"]
    assert addrs[0]["text"] == "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"


async def test_F3a_crypto_BTC_P2PKH(detector) -> None:
    spans = await _detect(detector, "Legacy BTC 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")
    addrs = [s for s in spans if s["canonical"] == "CRYPTO_ADDR"]
    assert addrs[0]["text"] == "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"


async def test_F3a_crypto_ETH(detector) -> None:
    spans = await _detect(detector, "ETH 0x742d35Cc6634C0532925a3b844Bc9e7595f0fA1c")
    addrs = [s for s in spans if s["canonical"] == "CRYPTO_ADDR"]
    assert addrs[0]["text"] == "0x742d35Cc6634C0532925a3b844Bc9e7595f0fA1c"


async def test_F3a_crypto_TRX(detector) -> None:
    spans = await _detect(detector, "Tron 钱包 TLPpXqaBSGmEZwgErJMtKDV3xLPocGnv3W")
    addrs = [s for s in spans if s["canonical"] == "CRYPTO_ADDR"]
    assert addrs[0]["text"] == "TLPpXqaBSGmEZwgErJMtKDV3xLPocGnv3W"


async def test_F3a_crypto_canonical_is_CRYPTO_ADDR(detector) -> None:
    """rename 验证：canonical 应为 CRYPTO_ADDR 而非旧 CRYPTO"""
    spans = await _detect(detector, "0x742d35Cc6634C0532925a3b844Bc9e7595f0fA1c")
    addrs = [s for s in spans if s["canonical"] == "CRYPTO_ADDR"]
    assert len(addrs) == 1
