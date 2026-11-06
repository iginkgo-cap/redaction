"""Evaluate Config D against ginkgo fixtures.

Usage:
    python tests/fixtures/ginkgo/evaluate_ginkgo.py [--service-url URL] [--json OUT]

Metrics:
    - Strict F1: exact span boundary + canonical match
    - Lenient F1: overlap + canonical match (over/under-extraction OK)
    - Binary F1: overlap, canonical ignored (business: as long as redacted)
    - FP-on-negatives: prediction overlaps a `negatives` token (true false positive)
    - Cross-turn consistency (E4): same literal appears in expected turns and is predicted
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from collections import defaultdict
from dataclasses import dataclass

import httpx

# Allow `from loader import ...` when run as script
HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))

from loader import (  # noqa: E402
    CANONICAL_KINDS,
    MultiTurnSample,
    SingleSample,
    Span,
    load_all,
    load_dir,
)


@dataclass
class PredSpan:
    start: int
    end: int
    text: str
    canonical: str
    source: str
    score: float


def call_detect(
    client: httpx.Client, url: str, text: str, scope: str | None = None
) -> tuple[list[PredSpan], int]:
    payload: dict = {"text": text}
    if scope:
        payload["scope"] = scope
    # timeout=120s：长文档 5000+ 字符 CPU 模式下 NVIDIA 需要 ~15-25s
    r = client.post(f"{url}/v1/detect", json=payload, timeout=120.0)
    r.raise_for_status()
    data = r.json()
    spans = [
        PredSpan(
            start=s["start"],
            end=s["end"],
            text=s["text"],
            canonical=s["canonical"],
            source=s["source"],
            score=s.get("score", 0.0),
        )
        for s in data["spans"]
    ]
    return spans, data.get("elapsed_ms", 0)


def overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return not (a_end <= b_start or b_end <= a_start)


def call_redact(
    client: httpx.Client, url: str, text: str, scope: str | None = None
) -> dict:
    """调 /v1/redact 返回 {redacted_text, mapping, spans, elapsed_ms}。
    mapping = [{placeholder, canonical, original, count}, ...]
    """
    payload: dict = {"text": text}
    if scope:
        payload["scope"] = scope
    # timeout=120s 与 detect 对齐
    r = client.post(f"{url}/v1/redact", json=payload, timeout=120.0)
    r.raise_for_status()
    return r.json()


# ─────────────────────────────────────────────────────────────
# #3 占位符一致性测试
# 两个维度：
#   (a) intra-text consistency: 同 sample 内同字面值 → 同 placeholder
#   (b) cross-turn consistency: 拼接 multi-turn → 同字面值 → 同 placeholder
# ─────────────────────────────────────────────────────────────

TURN_JOIN_SEP = "\n\n---\n\n"  # 拼接分隔符，不在普通文本中出现


def evaluate_placeholder_intra(
    sample: SingleSample, client: httpx.Client, url: str
) -> dict:
    """sample.text 调 /v1/redact，验证：
    (a) 每个 GT entity 的字面值都在 mapping 中（recall）
    (b) 每个 mapping 项的 placeholder 唯一（dedup correctness）—— 同字面值同 canonical
        只有 1 个 mapping 条目（service 内部保证；这里抓回潮）
    不验证 count（与 occurrence_index 语义不一致，无意义比较）。
    """
    r = call_redact(client, url, sample.text, scope=sample.scope)
    mapping_by_original = {m["original"]: m for m in r["mapping"]}
    checks = []
    # (a) recall via mapping
    seen: set[str] = set()
    for ent in sample.entities:
        if ent.text in seen:
            continue
        seen.add(ent.text)
        m = mapping_by_original.get(ent.text)
        status = "ok" if m is not None else "missed"
        checks.append({
            "id": sample.id, "text": ent.text, "canonical": ent.canonical,
            "status": status,
            "placeholder": m["placeholder"] if m else None,
        })
    # (b) dedup: 检查 mapping 中是否有同字面值多条（应该不会发生）
    originals = [m["original"] for m in r["mapping"]]
    if len(originals) != len(set(originals)):
        dups = [x for x in originals if originals.count(x) > 1]
        checks.append({
            "id": sample.id, "text": dups[0], "canonical": "N/A",
            "status": "dedup_violation", "placeholder": None,
        })
    return {"id": sample.id, "checks": checks}


def evaluate_placeholder_cross_turn(
    sample: MultiTurnSample, client: httpx.Client, url: str
) -> dict:
    """拼接 multi-turn → /v1/redact → 验证 cross_turn_refs 跨轮占位符一致。"""
    turn_texts = [t.text for t in sample.turns]
    joined = TURN_JOIN_SEP.join(turn_texts)
    r = call_redact(client, url, joined, scope=sample.scope)
    mapping = {m["original"]: m for m in r["mapping"]}
    redacted = r["redacted_text"]
    checks = []
    for ref in sample.cross_turn_refs:
        m = mapping.get(ref.text)
        expected_count = len(ref.appears_in_turns)
        if m is None:
            checks.append({
                "id": sample.id, "text": ref.text, "canonical": ref.canonical,
                "expected_turns": ref.appears_in_turns, "placeholder": None,
                "status": "missed", "expected_count": expected_count, "got_count": 0,
            })
            continue
        placeholder = m["placeholder"]
        # 拼接后的 redacted_text 里 placeholder 出现次数 == count；
        # 验证它至少出现 expected_count 次（实体可能在 turn 内还重复，所以 >= 即可）
        actual_count = redacted.count(placeholder)
        status = "ok" if actual_count >= expected_count else "under_count"
        checks.append({
            "id": sample.id, "text": ref.text, "canonical": ref.canonical,
            "expected_turns": ref.appears_in_turns, "placeholder": placeholder,
            "status": status, "expected_count": expected_count, "got_count": actual_count,
        })
    return {"id": sample.id, "checks": checks}


def aggregate_placeholder(
    intra_results: list[dict], cross_results: list[dict]
) -> dict:
    intra_checks = [c for r in intra_results for c in r["checks"]]
    cross_checks = [c for r in cross_results for c in r["checks"]]
    intra_ok = sum(1 for c in intra_checks if c["status"] == "ok")
    cross_ok = sum(1 for c in cross_checks if c["status"] == "ok")
    intra_fail = [c for c in intra_checks if c["status"] != "ok"]
    cross_fail = [c for c in cross_checks if c["status"] != "ok"]
    return {
        "intra_total": len(intra_checks),
        "intra_ok": intra_ok,
        "intra_pct": round(intra_ok / len(intra_checks) * 100, 1) if intra_checks else 0.0,
        "intra_failures": intra_fail,
        "cross_total": len(cross_checks),
        "cross_ok": cross_ok,
        "cross_pct": round(cross_ok / len(cross_checks) * 100, 1) if cross_checks else 0.0,
        "cross_failures": cross_fail,
    }


def evaluate_spans(
    preds: list[PredSpan],
    gts: list[Span],
    negatives_spans: list[tuple[int, int, str]],
) -> dict:
    """Compute strict/lenient/binary TP/FP/FN for one text."""
    gt_used = [False] * len(gts)
    pred_used = [False] * len(preds)
    strict_tp = lenient_tp = binary_tp = 0
    fp_on_neg = 0

    # Strict match: exact boundary + canonical
    for i, p in enumerate(preds):
        for j, g in enumerate(gts):
            if gt_used[j]:
                continue
            if p.start == g.start and p.end == g.end and p.canonical == g.canonical:
                strict_tp += 1
                pred_used[i] = True
                gt_used[j] = True
                break

    # Lenient (canonical match + overlap), only over unconsumed
    pred_used_l = pred_used.copy()
    gt_used_l = gt_used.copy()
    for i, p in enumerate(preds):
        if pred_used_l[i]:
            continue
        for j, g in enumerate(gts):
            if gt_used_l[j]:
                continue
            if p.canonical == g.canonical and overlaps(p.start, p.end, g.start, g.end):
                lenient_tp += 1
                pred_used_l[i] = True
                gt_used_l[j] = True
                break
    lenient_tp += strict_tp  # strict subset of lenient

    # Binary (overlap any, ignore canonical), independent counting
    pred_used_b = [False] * len(preds)
    gt_used_b = [False] * len(gts)
    for i, p in enumerate(preds):
        for j, g in enumerate(gts):
            if gt_used_b[j]:
                continue
            if overlaps(p.start, p.end, g.start, g.end):
                binary_tp += 1
                pred_used_b[i] = True
                gt_used_b[j] = True
                break

    # FP on negatives: any pred that overlaps a `negatives` span
    for p in preds:
        for ns, ne, _ntext in negatives_spans:
            if overlaps(p.start, p.end, ns, ne):
                fp_on_neg += 1
                break

    return {
        "n_gt": len(gts),
        "n_pred": len(preds),
        "strict_tp": strict_tp,
        "lenient_tp": lenient_tp,
        "binary_tp": binary_tp,
        "strict_fp": len(preds) - strict_tp,
        "lenient_fp": len(preds) - lenient_tp,
        "binary_fp": len(preds) - binary_tp,
        "strict_fn": len(gts) - strict_tp,
        "lenient_fn": len(gts) - lenient_tp,
        "binary_fn": len(gts) - binary_tp,
        "fp_on_negatives": fp_on_neg,
    }


def f1(tp: int, fp: int, fn: int) -> float:
    if tp == 0:
        return 0.0
    p = tp / (tp + fp)
    r = tp / (tp + fn)
    return 2 * p * r / (p + r) if (p + r) > 0 else 0.0


def precision(tp: int, fp: int) -> float:
    return tp / (tp + fp) if (tp + fp) > 0 else 0.0


def recall(tp: int, fn: int) -> float:
    return tp / (tp + fn) if (tp + fn) > 0 else 0.0


def resolve_negatives(text: str, negatives: list[str]) -> list[tuple[int, int, str]]:
    out: list[tuple[int, int, str]] = []
    for neg in negatives:
        start = 0
        while True:
            idx = text.find(neg, start)
            if idx < 0:
                break
            out.append((idx, idx + len(neg), neg))
            start = idx + 1
    return out


def evaluate_single(
    sample: SingleSample, client: httpx.Client, url: str
) -> tuple[dict, int]:
    preds, elapsed = call_detect(client, url, sample.text, scope=sample.scope)
    gts = sample.resolve_spans()
    neg_spans = resolve_negatives(sample.text, sample.negatives)
    metrics = evaluate_spans(preds, gts, neg_spans)
    metrics["id"] = sample.id
    metrics["scope"] = sample.scope
    metrics["language"] = sample.language
    metrics["elapsed_ms"] = elapsed
    # Per-canonical TP/FP/FN (lenient mode)
    per_canon_gt = defaultdict(int)
    per_canon_tp = defaultdict(int)
    per_canon_fp = defaultdict(int)
    for g in gts:
        per_canon_gt[g.canonical] += 1
    gt_used = [False] * len(gts)
    for p in preds:
        matched = False
        for j, g in enumerate(gts):
            if gt_used[j]:
                continue
            if p.canonical == g.canonical and overlaps(p.start, p.end, g.start, g.end):
                per_canon_tp[g.canonical] += 1
                gt_used[j] = True
                matched = True
                break
        if not matched and p.canonical in CANONICAL_KINDS:
            per_canon_fp[p.canonical] += 1
    metrics["per_canonical"] = {
        canon: {
            "tp": per_canon_tp[canon],
            "fp": per_canon_fp[canon],
            "fn": per_canon_gt[canon] - per_canon_tp[canon],
        }
        for canon in CANONICAL_KINDS
    }
    return metrics, elapsed


def evaluate_multi(
    sample: MultiTurnSample, client: httpx.Client, url: str
) -> tuple[list[dict], dict]:
    turn_results = []
    pred_by_turn: dict[int, list[PredSpan]] = {}
    elapsed_total = 0
    for turn in sample.turns:
        preds, elapsed = call_detect(client, url, turn.text, scope=sample.scope)
        pred_by_turn[turn.turn_id] = preds
        elapsed_total += elapsed
        # Build GT spans for this turn
        gts: list[Span] = []
        for ent in turn.entities:
            start = 0
            while True:
                idx = turn.text.find(ent.text, start)
                if idx < 0:
                    break
                gts.append(Span(idx, idx + len(ent.text), ent.text, ent.canonical))
                start = idx + 1
        metrics = evaluate_spans(preds, gts, [])
        metrics["id"] = sample.id
        metrics["turn_id"] = turn.turn_id
        metrics["scope"] = sample.scope
        metrics["language"] = sample.language
        metrics["elapsed_ms"] = elapsed
        turn_results.append(metrics)

    # Cross-turn consistency: for each ref, count how many of the listed turns
    # actually had it detected (overlap with text).
    ref_results = []
    for ref in sample.cross_turn_refs:
        per_turn_hit = []
        for tid in ref.appears_in_turns:
            preds = pred_by_turn.get(tid, [])
            turn_text = next(t.text for t in sample.turns if t.turn_id == tid)
            idx = turn_text.find(ref.text)
            if idx < 0:
                per_turn_hit.append(False)
                continue
            hit = any(
                p.canonical == ref.canonical
                and overlaps(p.start, p.end, idx, idx + len(ref.text))
                for p in preds
            )
            per_turn_hit.append(hit)
        ref_results.append(
            {
                "id": sample.id,
                "text": ref.text,
                "canonical": ref.canonical,
                "expected_turns": ref.appears_in_turns,
                "hit_count": sum(per_turn_hit),
                "total": len(per_turn_hit),
                "consistent": all(per_turn_hit),
            }
        )

    return turn_results, {
        "id": sample.id,
        "scope": sample.scope,
        "elapsed_ms_total": elapsed_total,
        "cross_turn_refs": ref_results,
    }


def aggregate(rows: list[dict]) -> dict:
    keys = ("strict", "lenient", "binary")
    sums = {k: {"tp": 0, "fp": 0, "fn": 0} for k in keys}
    fp_neg = 0
    for r in rows:
        for k in keys:
            sums[k]["tp"] += r[f"{k}_tp"]
            sums[k]["fp"] += r[f"{k}_fp"]
            sums[k]["fn"] += r[f"{k}_fn"]
        fp_neg += r.get("fp_on_negatives", 0)
    out = {}
    for k in keys:
        s = sums[k]
        out[k] = {
            "tp": s["tp"],
            "fp": s["fp"],
            "fn": s["fn"],
            "precision": round(precision(s["tp"], s["fp"]) * 100, 2),
            "recall": round(recall(s["tp"], s["fn"]) * 100, 2),
            "f1": round(f1(s["tp"], s["fp"], s["fn"]) * 100, 2),
        }
    out["fp_on_negatives"] = fp_neg
    return out


def aggregate_canonical(rows: list[dict]) -> dict:
    per_canon: dict[str, dict[str, int]] = defaultdict(
        lambda: {"tp": 0, "fp": 0, "fn": 0}
    )
    for r in rows:
        for canon, c in r.get("per_canonical", {}).items():
            per_canon[canon]["tp"] += c["tp"]
            per_canon[canon]["fp"] += c["fp"]
            per_canon[canon]["fn"] += c["fn"]
    out = {}
    for canon in CANONICAL_KINDS:
        c = per_canon.get(canon, {"tp": 0, "fp": 0, "fn": 0})
        gt_total = c["tp"] + c["fn"]
        out[canon] = {
            **c,
            "gt_total": gt_total,
            "precision": round(precision(c["tp"], c["fp"]) * 100, 2),
            "recall": round(recall(c["tp"], c["fn"]) * 100, 2),
            "f1": round(f1(c["tp"], c["fp"], c["fn"]) * 100, 2),
        }
    return out


def aggregate_scope(rows: list[dict]) -> dict:
    by_scope: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_scope[r["scope"]].append(r)
    return {scope: aggregate(rs) for scope, rs in by_scope.items()}


def print_report(report: dict) -> None:
    print()
    print("=" * 72)
    print("Ginkgo Fixtures · Config D Baseline")
    print("=" * 72)

    print("\n[Overall, all samples]")
    o = report["overall"]
    for mode in ("strict", "lenient", "binary"):
        m = o[mode]
        print(
            f"  {mode:8s}  P={m['precision']:5.1f}%  R={m['recall']:5.1f}%  "
            f"F1={m['f1']:5.1f}%   (TP={m['tp']}  FP={m['fp']}  FN={m['fn']})"
        )
    print(f"  FP on negatives (公开股票/金额误识)：{o['fp_on_negatives']}")

    print("\n[By canonical] (lenient mode)")
    print(f"  {'Canonical':<14} {'GT':>5} {'TP':>5} {'FP':>5} {'FN':>5} "
          f"{'P%':>7} {'R%':>7} {'F1%':>7}")
    for canon in sorted(CANONICAL_KINDS):
        c = report["by_canonical"][canon]
        if c["gt_total"] == 0 and c["tp"] == 0 and c["fp"] == 0:
            continue
        print(
            f"  {canon:<14} {c['gt_total']:>5} {c['tp']:>5} {c['fp']:>5} "
            f"{c['fn']:>5} {c['precision']:>7.1f} {c['recall']:>7.1f} "
            f"{c['f1']:>7.1f}"
        )

    print("\n[By scope] (lenient mode)")
    print(f"  {'Scope':<20} {'TP':>5} {'FP':>5} {'FN':>5} "
          f"{'P%':>7} {'R%':>7} {'F1%':>7}")
    for scope, agg in sorted(report["by_scope"].items()):
        m = agg["lenient"]
        print(
            f"  {scope:<20} {m['tp']:>5} {m['fp']:>5} {m['fn']:>5} "
            f"{m['precision']:>7.1f} {m['recall']:>7.1f} {m['f1']:>7.1f}"
        )

    print("\n[Latency]")
    print(
        f"  min={report['latency']['min']:.0f}ms  "
        f"p50={report['latency']['p50']:.0f}ms  "
        f"p95={report['latency']['p95']:.0f}ms  "
        f"max={report['latency']['max']:.0f}ms  "
        f"avg={report['latency']['avg']:.0f}ms"
    )

    cross = report["cross_turn"]
    if cross["total_refs"]:
        print("\n[Cross-turn consistency · E4 (detect-level)]")
        print(
            f"  consistent {cross['consistent_refs']}/{cross['total_refs']} "
            f"({cross['consistent_pct']:.1f}%)  "
            f"hit rate across all expected turns: {cross['hit_rate_pct']:.1f}%"
        )
        if cross["failures"]:
            print("  失败 case：")
            for f in cross["failures"][:5]:
                print(
                    f"    - {f['id']}  {f['text']!r} ({f['canonical']}) "
                    f"hit {f['hit_count']}/{f['total']}"
                )
            if len(cross["failures"]) > 5:
                print(f"    ... 还有 {len(cross['failures']) - 5} 条")

    # #3 placeholder consistency report
    ph = report["placeholder"]
    print("\n[Placeholder consistency · /v1/redact]")
    print(
        f"  intra-text  {ph['intra_ok']}/{ph['intra_total']} ({ph['intra_pct']:.1f}%)  "
        f"— 同 sample 内每个 GT entity 是否得到 placeholder"
    )
    print(
        f"  cross-turn  {ph['cross_ok']}/{ph['cross_total']} ({ph['cross_pct']:.1f}%)  "
        f"— 拼接 multi-turn 后跨轮占位符是否复用"
    )
    if ph["cross_failures"]:
        print("  跨轮失败 case（top 5）：")
        for f in ph["cross_failures"][:5]:
            print(
                f"    - {f['id']}  {f['text']!r} ({f['canonical']}) "
                f"placeholder={f['placeholder']}  "
                f"出现 {f['got_count']}/{f['expected_count']}  [{f['status']}]"
            )
    print()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service-url", default="http://localhost:8001")
    parser.add_argument("--json", help="write full report to this JSON file")
    parser.add_argument(
        "--fixture-dir",
        default=None,
        help="评测指定目录下的 *.jsonl（默认用 ginkgo load_all）；"
        "用于 public_en / public_zh 等额外集",
    )
    args = parser.parse_args()

    if args.fixture_dir:
        singles, multis = load_dir(pathlib.Path(args.fixture_dir))
    else:
        singles, multis = load_all()
    print(
        f"Loaded {len(singles)} single samples + {len(multis)} multi-turn "
        f"({sum(len(m.turns) for m in multis)} turns)."
    )
    print(f"Calling {args.service_url} ...")

    all_rows: list[dict] = []
    elapsed_list: list[int] = []

    placeholder_intra_results: list[dict] = []
    placeholder_cross_results: list[dict] = []

    with httpx.Client(trust_env=False, timeout=60.0) as client:
        t0 = time.time()

        for i, s in enumerate(singles, 1):
            try:
                metrics, elapsed = evaluate_single(s, client, args.service_url)
                all_rows.append(metrics)
                elapsed_list.append(elapsed)
                # #3 placeholder intra-text consistency
                placeholder_intra_results.append(
                    evaluate_placeholder_intra(s, client, args.service_url)
                )
            except Exception as e:
                print(f"  [WARN] {s.id} failed: {e}", file=sys.stderr)
            if i % 10 == 0:
                print(f"  ... single {i}/{len(singles)}")

        all_cross: list[dict] = []
        for j, m in enumerate(multis, 1):
            try:
                turn_rows, cross = evaluate_multi(m, client, args.service_url)
                all_rows.extend(turn_rows)
                elapsed_list.extend(r["elapsed_ms"] for r in turn_rows)
                all_cross.append(cross)
                # #3 placeholder cross-turn consistency
                placeholder_cross_results.append(
                    evaluate_placeholder_cross_turn(m, client, args.service_url)
                )
            except Exception as e:
                print(f"  [WARN] {m.id} failed: {e}", file=sys.stderr)
            print(f"  ... multi {j}/{len(multis)}")

        wall = time.time() - t0
        print(f"\nDone in {wall:.1f}s ({len(all_rows)} API calls).")

    overall = aggregate(all_rows)
    by_canon = aggregate_canonical([r for r in all_rows if "per_canonical" in r])
    by_scope = aggregate_scope(all_rows)

    elapsed_sorted = sorted(elapsed_list)
    n = len(elapsed_sorted)
    latency = {
        "min": elapsed_sorted[0] if n else 0,
        "max": elapsed_sorted[-1] if n else 0,
        "p50": elapsed_sorted[n // 2] if n else 0,
        "p95": elapsed_sorted[int(n * 0.95)] if n else 0,
        "avg": sum(elapsed_list) / n if n else 0,
    }

    refs = [ref for c in all_cross for ref in c["cross_turn_refs"]]
    cross_summary = {
        "total_refs": len(refs),
        "consistent_refs": sum(1 for r in refs if r["consistent"]),
        "consistent_pct": (
            sum(1 for r in refs if r["consistent"]) / len(refs) * 100 if refs else 0
        ),
        "hit_rate_pct": (
            sum(r["hit_count"] for r in refs)
            / sum(r["total"] for r in refs)
            * 100
            if refs
            else 0
        ),
        "failures": [r for r in refs if not r["consistent"]],
    }

    placeholder_summary = aggregate_placeholder(
        placeholder_intra_results, placeholder_cross_results
    )

    report = {
        "overall": overall,
        "by_canonical": by_canon,
        "by_scope": by_scope,
        "latency": latency,
        "cross_turn": cross_summary,
        "placeholder": placeholder_summary,
        "n_samples": len(all_rows),
    }

    print_report(report)

    if args.json:
        outpath = pathlib.Path(args.json)
        outpath.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"Full report written to: {outpath}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
