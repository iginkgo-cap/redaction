"""canonical.py 单测：YAML 加载与映射查询。"""
from __future__ import annotations


def test_hanlp_映射保留_4_类(canonical_map) -> None:
    assert canonical_map.lookup("hanlp", "PERSON") == "PERSON"
    assert canonical_map.lookup("hanlp", "ORG") == "ORG"
    assert canonical_map.lookup("hanlp", "LOC") == "ADDRESS"
    assert canonical_map.lookup("hanlp", "FAC") == "ADDRESS"


def test_hanlp_其他类全部_drop(canonical_map) -> None:
    drops = ["NORP", "GPE", "PRODUCT", "EVENT", "WORK_OF_ART", "LAW",
             "LANGUAGE", "DATE", "TIME", "PERCENT", "MONEY",
             "QUANTITY", "ORDINAL", "CARDINAL"]
    for d in drops:
        assert canonical_map.lookup("hanlp", d) is None, f"{d} 不应被映射"


def test_gliner_只传_3_个_NER_label(canonical_map) -> None:
    """GLiNER 候选（nvidia/knowledgator 共用此段）只传 3 个 NER 真正擅长的语义类
    （person/address/org）。结构化 PII（phone/email/id/bank card）由正则严格覆盖、
    显式不传给模型，避免中文短文本高频误报。Round 9 起 knowledgator 复用此契约。"""
    labels = canonical_map.nvidia_labels
    assert len(labels) == 3
    assert set(labels) == {"person name", "home address", "organization"}
    # 结构化 / 已下线类均不向模型传入
    assert "ip address" not in labels
    assert "social security number" not in labels
    assert "crypto wallet address" not in labels
    assert "phone number" not in labels


def test_en_regex_映射_含_PHONE_INTL(canonical_map) -> None:
    """D11 回归 IP 后 en_regex 基础映射；S5 (round11) URL 拆为敏感特征三条"""
    expected = {
        "EMAIL": "EMAIL",
        "URL_USERINFO": "URL",
        "URL_CREDENTIAL": "URL",
        "URL_INTERNAL": "URL",
        "CREDIT_CARD": "BANK_CARD",
        "IBAN": "BANK_CARD",
        "SWIFT_BIC": "BANK_CARD",
        "CRYPTO_ADDR": "CRYPTO_ADDR",
        "PASSPORT_GENERIC": "ID_CARD",
        "PHONE_INTL": "PHONE",
        "IP_ADDRESS": "IP_ADDRESS",
    }
    for raw, canonical in expected.items():
        assert canonical_map.lookup("en_regex", raw) == canonical


def test_cn_regex_映射(canonical_map) -> None:
    assert canonical_map.lookup("cn_regex", "CN_PHONE") == "PHONE"
    assert canonical_map.lookup("cn_regex", "CN_EMAIL") == "EMAIL"
    assert canonical_map.lookup("cn_regex", "CN_ID_CARD") == "ID_CARD"
    assert canonical_map.lookup("cn_regex", "CN_BANK_CARD") == "BANK_CARD"


def test_unknown_source_返回_None(canonical_map) -> None:
    assert canonical_map.lookup("unknown_source", "PERSON") is None


def test_hanlp_角色词黑名单(canonical_map) -> None:
    assert "甲方" in canonical_map.hanlp_role_blacklist
    assert "出借方" in canonical_map.hanlp_role_blacklist
    assert "张三" not in canonical_map.hanlp_role_blacklist  # 正常人名不在黑名单
