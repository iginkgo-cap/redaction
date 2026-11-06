# Ginkgo 家办金融场景 PII 测试集

> 用途：评估 redaction-service 在 Ginkgo Labs 真实业务文本上的识别覆盖率、误识率、跨轮一致性。
> 公开测试集（CLUENER / SPY / ai4privacy）已覆盖通用 PII；本目录补足家办金融场景空白。
> 创建日期：2026-05-21

---

## 文件清单

| 文件 | 场景 | 样本数 | 主要测试维度 |
|---|---|---|---|
| `e1_ic_memo.jsonl` | 投决备忘 / IC 纪要 | ~12 | PERSON / ORG / BUSINESS_ID / 公开股票代码（不脱敏对照） |
| `e2_remittance_email.jsonl` | 跨境汇款邮件 | ~12 | EMAIL / BANK_CARD(IBAN/SWIFT) / ACCOUNT / PERSON |
| `e3_kyc_beneficiary.jsonl` | KYC / 受益人结构 | ~12 | ID_CARD / USCC / PERSON / ORG / 持股% |
| `e4_lark_multiturn.jsonl` | Lark 中文多轮 chat | 5 组 × 5 轮 | 跨轮 PERSON / 客户号 一致性 |
| `e5_contract.jsonl` | 中文合同片段 | ~10 | contract_id / USCC / 签约方 ORG / 金额 |
| `e6_crypto_holding.jsonl` | 加密资产持仓邮件 | ~8 | CRYPTO_ADDR (BTC bech32/P2PKH / ETH EIP-55) |
| `e7_offshore.jsonl` | 港澳台 / 跨境家办 | ~10 | 繁体姓名 / 香港 ID / 离岸 SPV |
| `e8_internal_systems.jsonl` | 客户号 / 内部账号 | ~9 | 客户号 / 业务编号 / 内部 ID 体系 |
| `e9_long_documents.jsonl` | 长文档（5000+ 字）| 5 | 长文 召回 / 跨段实体一致性 |
| `e10_ocr_variants.jsonl` | OCR 噪声变体 | ~12 | OCR 错位 / 半角全角 / 错切 |
| **`e11_medium_500_800.jsonl`** | **500-800 字 · IC / 汇款 / 客户拜访 / 加密询价 / KYC 简版** | **10** | **中等文本桶 · 多场景** |
| **`e12_medium_800_1500.jsonl`** | **800-1500 字 · KYC 综合 / 受益人结构 / 合同 / 邮件链 / 加密资产** | **10** | **中等文本桶 · 综合场景** |
| **`e13_long_1500_2500.jsonl`** | **1500-2500 字 · 股权转让 / 离岸 SPV / 多方家办 / KYC 完整 / 加密托管** | **5** | **长文本桶 · 多 PII 类型组合** |
| **`e14_long_2500_3500.jsonl`** | **2500-3500 字 · 综合 KYC / 完整 IC 备忘 / 多家办联动** | **3** | **超长文本桶 · 综合场景** |

> 加粗为 2026-05-29 新增（e11-e14），补齐 500-3500 字桶（共 28 个新样本）；详见 [`docs/TEST-SET-SPEC.md`](../../../docs/TEST-SET-SPEC.md)。

---

## 字数桶分布（截至 2026-05-29）

按 `length_bucket` 字段或 `len(text)` 实测：

| Bucket | 现有样本 | 主要来源 |
|---|---:|---|
| `<200` | ~75 | e1 / e10 / 部分 e2-e8 |
| `200-300` | ~25 | e2 / e3 / e7 |
| `300-500` | ~10 | e6 / e8 |
| **`500-800`** | **10** | **e11**（新增） |
| **`800-1500`** | **10** | **e12**（新增） |
| **`1500-2500`** | **5** | **e13**（新增） |
| **`2500-3500`** | **3** | **e14**（新增） |
| `5000+` | 5 | e9 |

> ⚠️ 当前 `3500-5000` 桶仍为空白；如未来需补齐，请参考 e14 的样本模式扩展。
> 已知缺口与 SLO 对应关系见 [`docs/REQUIREMENTS-AND-SLO.md`](../../../docs/REQUIREMENTS-AND-SLO.md) §3。

---

## 10 类 Canonical（与 service 对齐）

`PERSON` `PHONE` `EMAIL` `ID_CARD` `BANK_CARD` `ACCOUNT` `CRYPTO_ADDR` `ORG` `BUSINESS_ID` `ADDRESS`

> 公开市场代码（A股 / 港股 / 美股 / 公募基金 / ETF / 指数）**不脱敏**，但保留在文本中用于"不应误识"对照测试。

---

## 数据格式

### 单段（E1 / E2 / E3 / E5 / E6 / E7 / E11-E14）

```jsonl
{
  "id": "ginkgo-e11-001",
  "scope": "ic_memo",
  "language": "zh",
  "length_bucket": "500-800",
  "text": "...",
  "entities": [
    {"text": "王志远", "canonical": "PERSON", "note": "CIO"},
    {"text": "13800138001", "canonical": "PHONE"}
  ],
  "negatives": ["600519.SH", "贵州茅台"]
}
```

> ⚠️ E11-E14 引入新字段 `length_bucket`（枚举 `<200` / `200-300` / `300-500` / `500-800` / `800-1500` / `1500-2500` / `2500-3500` / `3500-5000` / `5000+`），早期 E1-E10 无此字段，由 `loader.py` 按 `len(text)` 自动归桶。

字段说明：
- `id` — 全局唯一，格式 `ginkgo-<bucket>-<seq>`
- `scope` — 用于 service `/v1/detect` 的 scope 入参（ic_memo / email / kyc / chat / contract / crypto / offshore）
- `language` — `zh` / `en` / `mixed`
- `text` — 原始文本
- `entities` — 期望识别为 PII 的 ground truth 列表（顺序无关，按 text 出现顺序）
- `negatives` — 期望**不应**被识别为 PII 的 token（公开股票代码 / 公募基金代码 / 公开金额数字等），用于精确率回归

### 多轮（E4）

```jsonl
{
  "id": "ginkgo-e4-conv-001",
  "scope": "lark_chat",
  "language": "zh",
  "turns": [
    {"turn_id": 1, "role": "alice", "text": "...", "entities": [...]},
    {"turn_id": 2, "role": "bob", "text": "...", "entities": [...]}
  ],
  "cross_turn_refs": [
    {"text": "王总", "canonical": "PERSON", "appears_in_turns": [1, 3, 5], "note": "跨轮一致性：必须分配同一占位符 N"}
  ]
}
```

`cross_turn_refs` 用于断言 Phase 2 Node pipeline 的跨轮去重：同一 PERSON / 客户号在不同 turn 出现时，占位符 N 必须复用，不能新递增。

---

## span 自动定位约定

为减少手工标注 offset 出错，本测试集**不写 start/end**，由 `loader.py` 在加载时通过 `text.find(entity_text)` 自动定位：

- 单 entity 出现一次：取唯一匹配
- 单 entity 出现多次（如同一 PERSON 在文本中重复）：返回全部匹配 span 列表
- 0 次匹配：`validate_fixtures.py` 报错

只有需要"同一字面值在文本里出现多次但只算一次"这类边界 case，才在 entity 上加 `occurrence_index` 字段显式选第 N 个匹配。本测试集尽量避免此类构造。

---

## 评测指标（loader.py + 评测脚本对接 service 后产出）

| 指标 | 含义 |
|---|---|
| Recall | TP / (TP + FN)，按 canonical 类拆分 |
| Precision | TP / (TP + FP)，FP 含命中 `negatives` 列表的项 |
| Binary F1 | 忽略 canonical，只看 span 是否被识别 |
| Strict F1 | canonical + span 同时精确匹配 |
| 跨轮一致性（仅 E4）| `cross_turn_refs` 中所有 turn 内同一字面值应分配相同占位符 N |

---

## License & 隐私

- 本测试集**100% 合成数据**，所有姓名、电话、身份证、银行卡、IBAN、邮箱、地址均为人工编造或参考公开 test 数字（如 Visa 测试卡 4111-1111-1111-1111），不含任何真实客户信息
- 合成时刻意避开 Ginkgo 真实客户姓名 / 真实合作机构名（如有重名纯属巧合）
- 仓库内部使用，**不对外发布**
- License：Internal-Only（与 redaction-service 主仓库一致）

---

## 修订记录

| 日期 | 改动 |
|---|---|
| 2026-05-21 | 初版：E1-E7 七大金融场景 |
| 2026-05-22 | 追加 E8 / E9 / E10（内部系统 / 长文档 / OCR 变体） |
| 2026-05-29 | 追加 E11-E14（共 28 个新样本，补齐 500-3500 字桶）+ 引入 `length_bucket` 字段；详见 [`docs/TEST-SET-SPEC.md`](../../../docs/TEST-SET-SPEC.md) |
| 2026-05-29 | Round 9：追加 **E15 金融英文集**（18 条，scope 以 `_en` 结尾，覆盖 12 类 canonical）；**E11-E15 全部注册进 `loader.load_all`**（此前 E11-E14 漏注册）；新增公开集 `tests/fixtures/public_en/`（ai4privacy 87 样本）与 `public_zh/`（peoplesdaily 100 样本），经 `--fixture-dir` 单独评测，口径映射见 [`reports/round9/round9-public-set-taxonomy-mapping.md`](../../../reports/round9/round9-public-set-taxonomy-mapping.md) |
