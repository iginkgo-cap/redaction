"""服务配置。从环境变量读取，缺省值适合本地开发。"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REDACTION_",
        env_file=".env",
        extra="ignore",
    )

    # 模型加载
    model_dir: Path = Path(__file__).resolve().parent.parent / "models"
    config_dir: Path = Path(__file__).resolve().parent.parent / "config"
    gpu_device: str = "auto"  # auto | cpu | mps | cuda

    # 触发阈值
    hanlp_min_zh_chars: int = 10
    nvidia_chunk_chars: int = 800
    nvidia_chunk_overlap: int = 80
    hanlp_chunk_chars: int = 1000
    nvidia_threshold: float = 0.4
    # GLiNER 候选（knowledgator/urchade…，走 gliner_generic）独立阈值。
    # Round 9 横评：knowledgator-base @0.5 是 FP-neg 甜点（108→92，Binary 仅 -0.8）。
    # 与 nvidia_threshold(0.4) 分开，避免换主力模型时污染 nvidia 兜底语义。
    gliner_threshold: float = 0.5
    # GLiNER 候选独立 chunk（与 nvidia_chunk_chars 解耦的可调旋钮）。
    # 背景：knowledgator-base 窗口 max_len=2048 token，是 NVIDIA(384 token) 的 ~5.3×，
    # 共享的 800 字只用掉它窗口的 ~1/3，故拆出独立配置便于按模型调；**不能**直接调大
    # nvidia_chunk_chars（NVIDIA max_len 仅 384 token，800 字中文≈666 token 已在静默截断，
    # 仅因其只兜底英文、CJK span 被 _cjk_ratio 丢弃而未暴露）。
    # 默认 800：2026-05-31 实测把 knowledgator chunk 提到 1800 字，长文延迟 -2.7%(噪声内)、
    # 命中 span 数每档完全相同（召回无变化）——GLiNER CPU 成本 ∝ 总 token 量、与切法几乎无关，
    # 大 chunk 的 attention O(n²) 项还略亏。故默认维持 800，旋钮保留供真实长文档（实体压边界）调优。
    gliner_chunk_chars: int = 800
    gliner_chunk_overlap: int = 80

    # 单 detector 超时（秒）
    detector_timeout_sec: float = 30.0

    # 推理准入（2026-10-19 OOM 修复）：模型 detector（hanlp/knowledgator…）同时跑的上限，
    # 以及排队中的请求上限——超过直接 503，让调用方快速降级，而不是堆满线程池把内存撑爆。
    # 线上实测：无上限时一次工具结果脱敏可打进 400+ 并发请求，RSS 从 2.9G 顶到 4G 被 OOM 杀。
    max_concurrent_inference: int = 4
    max_pending_requests: int = 6
    # 单请求文本上限（字符）；labs 侧分块 ≤ 80k，这里只做兜底
    max_text_chars: int = 100_000

    # CPU 上多个 torch 模型 asyncio 并行会内存带宽/缓存争抢，反而更慢甚至超时
    # （Round 9 实测：HanLP∥GLiNER 并行 medium 15s vs 串行 ~2.6s）。
    # 串行执行让每个 detector 独占资源（regex 极快可忽略）。
    serial_detectors: bool = False

    # v2 单次请求总字数上限（ADR-002 §2.5），超过返回 text_too_large
    max_request_chars: int = 200_000

    # v2 file 项（LOOK-6876）：解码后字节上限（base64 后约 32,000,000，留余量在 nginx client_max_body_size 32m 内）、
    # 页数上限；超过返回 file_too_large。PDF 抽字与写回另设并发上限，不占推理槽（ADR-007 §5）
    max_file_bytes: int = 24_000_000
    max_file_pages: int = 300
    pdf_concurrency: int = 1

    # v2 映射库（ADR-007）：指向控制库 redaction_meta，各 namespace 的库 rs_<id> 用同一账号、在同一实例上。
    # 不配则无状态模式：只有 detect 可用
    database_url: str | None = None
    # 首次使用 namespace 时自动建库；私有化客户 DBA 不给建库权限时关掉，由客户预建 rs_<id>
    database_auto_create: bool = True
    # namespace 与调用方登记文件；不配用 config_dir/namespaces.yaml（私有化部署挂载自己的文件）
    namespaces_file: Path | None = None
    # 密钥来源：secretsmanager（读 key_secret_id）| local（读 key_file，没有再读下面两个环境变量）
    key_source: Literal["secretsmanager", "local"] = "local"
    key_secret_id: str | None = None
    key_file: Path | None = None
    encryption_key: str | None = None  # base64 32 字节
    hmac_key: str | None = None  # base64 32 字节

    # 日志
    log_level: str = "INFO"

    # 模型路径覆盖（不设则用 HF 缓存）
    nvidia_model_path: str | None = None

    # 活跃 detector 组合（逗号分隔）。Round 9 横评用 REDACTION_DETECTORS 切换，
    # 让同一镜像跑不同模型/组合，无需改代码。
    # Round 9 定稿（D9）：英文/多语 NER 主力 nvidia → knowledgator-base（ONNX-INT8）。
    # 分布外金融英文最准(91.5)、Apache、ONNX CPU 快 ~3.7×、内存省 ~2GB。nvidia 退为离线兜底候选。
    # Round 11（S7）：+gazetteer 人名补召回（纯名单查表，微秒级，无模型开销）。
    # LOOK-5231：默认不含 hanlp——HanLP 模型是 CC BY-NC-SA 4.0（非商用），对外版本不能带。
    # 内部部署在 docker-compose 里显式写 REDACTION_DETECTORS=...,hanlp,...，镜像构建传 WITH_HANLP=1。
    detectors: str = "cn_regex,en_regex,knowledgator,gazetteer"

    # /v1/* 不鉴权。内部默认开（labs 还在用 /v1/detect，LOOK-6877 下线），客户版部署设 false
    enable_v1: bool = True

    @property
    def detector_names(self) -> list[str]:
        from .detectors import parse_detector_names

        return parse_detector_names(self.detectors)


settings = Settings()
