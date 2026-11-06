"""模型预下载脚本。

用途：本地 / CI / 部署时预先下载模型到指定目录（模型不进镜像）。

Round 9 定稿（D9）：英文/多语 NER 主力 nvidia → knowledgator-base。knowledgator 只拉
**ONNX-INT8 最小集 ~198MB**（quint8 + tokenizer + config），不拉 pytorch_model.bin / fp32 /
fp16 共 ~1.6GB 冗余。nvidia 退为离线兜底候选，按需 `--only nvidia` 单独拉。

LOOK-5231：默认只下 knowledgator。HanLP 模型是 CC BY-NC-SA 4.0（非商用），要显式 --with-hanlp
（内部部署用；镜像也要带 WITH_HANLP=1 构建）。国内网络设 HF_ENDPOINT=https://hf-mirror.com 走镜像站。

用法：
  python scripts/download_models.py [--target /opt/models]      # knowledgator
  python scripts/download_models.py --with-hanlp                # hanlp + knowledgator（内部）
  python scripts/download_models.py --only nvidia               # 离线兜底候选
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# knowledgator ONNX 推理所需的最小文件集（不含 pytorch_model.bin / model.onnx / model_fp16.onnx）。
_KNOWLEDGATOR_REPO = "knowledgator/gliner-pii-base-v1.0"
_KNOWLEDGATOR_FILES = [
    "onnx/model_quint8.onnx",   # 188M — INT8 量化推理图（唯一需要的权重）
    "gliner_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "added_tokens.json",
    "spm.model",
]


def download_hanlp(target: Path) -> None:
    print(f"[hanlp] downloading MTL ELECTRA Base to {target}/.hanlp ...")
    os.environ["HANLP_HOME"] = str(target / ".hanlp")
    import hanlp

    hanlp.load(hanlp.pretrained.mtl.CLOSE_TOK_POS_NER_SRL_DEP_SDP_CON_ELECTRA_BASE_ZH)
    print("[hanlp] done")


def download_knowledgator(target: Path) -> None:
    """精确拉 quint8 最小集（~198MB），避免 GLiNER.from_pretrained 顺带拉 ~1.6GB 冗余权重。"""
    print(f"[knowledgator] downloading ONNX-INT8 minimal set to {target}/huggingface ...")
    os.environ["HF_HOME"] = str(target / "huggingface")
    from huggingface_hub import hf_hub_download

    for fname in _KNOWLEDGATOR_FILES:
        path = hf_hub_download(repo_id=_KNOWLEDGATOR_REPO, filename=fname)
        print(f"[knowledgator]   {fname} -> {path}")
    print("[knowledgator] done (quint8 minimal set)")


def download_nvidia(target: Path) -> None:
    print(f"[nvidia] downloading gliner-PII to {target}/huggingface ...")
    os.environ["HF_HOME"] = str(target / "huggingface")
    from gliner import GLiNER

    GLiNER.from_pretrained("nvidia/gliner-PII")
    print("[nvidia] done")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=str(Path.home()), help="模型存储根目录（默认 $HOME）")
    ap.add_argument(
        "--only", choices=["hanlp", "knowledgator", "nvidia"], help="只下载某一项"
    )
    ap.add_argument("--with-hanlp", action="store_true", help="默认下载时额外下 HanLP（模型非商用）")
    args = ap.parse_args()

    target = Path(args.target).resolve()
    target.mkdir(parents=True, exist_ok=True)

    try:
        if args.only is None:
            # 默认：在线主链路所需（knowledgator 英文/多语；内部加 --with-hanlp 下中文 HanLP）
            if args.with_hanlp:
                download_hanlp(target)
            download_knowledgator(target)
        elif args.only == "hanlp":
            download_hanlp(target)
        elif args.only == "knowledgator":
            download_knowledgator(target)
        elif args.only == "nvidia":
            download_nvidia(target)
    except Exception as exc:  # noqa: BLE001
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    print("[ok] all models ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
