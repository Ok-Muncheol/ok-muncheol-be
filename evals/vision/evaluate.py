"""정답 카드(JSONL)와 예측(JSONL)을 칸별로 채점한다.

예측 한 줄: {"example_id": ..., "fields": {칸: {"value": ..., "model_confidence": ...}}, ...}
정답이 None인 칸(라벨 없음·매핑 불가)은 채점에서 뺀다. 3인칭 영상은 A/B 순서를 모델이 알 수 없으므로
A/B 쌍 칸은 맞바꿔도 맞은 것으로 본다. 결과는 무시 경로 evals/results/ 에 둔다.

    python -m evals.vision.evaluate --gt data/private/aihub/vision_gt.jsonl \
        --pred evals/results/vision_pred_adaptive.jsonl \
        --out evals/results/vision_report_adaptive.json
"""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

PAIR_FIELDS: tuple[tuple[str, str], ...] = (("movement_A", "movement_B"), ("signal_A", "signal_B"))
CONFIDENCE_BUCKETS: tuple[float, ...] = (0.5, 0.7, 0.9)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _pred_value(pred: dict, key: str) -> tuple[object, float | None]:
    field = (pred.get("fields") or {}).get(key) or {}
    return field.get("value"), field.get("model_confidence")


def _swapped_match(gt: dict[str, object], pred: dict, key: str) -> bool:
    for a, b in PAIR_FIELDS:
        if key in (a, b):
            other = b if key == a else a
            return _pred_value(pred, other)[0] == gt.get(key)
    return False


def _bucket(confidence: float | None) -> str:
    if confidence is None:
        return "none"
    for edge in CONFIDENCE_BUCKETS:
        if confidence < edge:
            return f"<{edge}"
    return f">={CONFIDENCE_BUCKETS[-1]}"


def is_simple_scope(fields: dict) -> bool:
    """유턴이 끼거나 둘 다 우회전인 드문 유형은 단순 범위에서 뺀다(100건 중 7건, 진행 0~0.33)."""
    moves = (fields.get("movement_A"), fields.get("movement_B"))
    return "u_turn" not in moves and moves != ("right_turn", "right_turn")


def select_rows(gt_rows: list[dict], mvp_only: bool = True, min_sharpness: float | None = None,
                simple_scope: bool = False, include_duplicates: bool = False) -> list[dict]:
    """채점 대상 정답 행. 화질 게이트(video_quality.sharpness)와 단순 범위를 같이 적용한다.
    중복 영상(dedupe.py 표시)은 기본으로 뺀다. 라벨이 충돌하는 그룹은 전부 빼고,
    같은 라벨 그룹은 대표 하나만 남긴다."""
    from evals.vision.quality import passes_gate

    rows = [r for r in gt_rows if not mvp_only or r.get("in_mvp_scope")]
    if not include_duplicates:
        rows = [r for r in rows if not r.get("duplicate_conflict") and not r.get("duplicate_of")]
    if min_sharpness is not None:
        rows = [r for r in rows if passes_gate(r, min_sharpness)]
    if simple_scope:
        rows = [r for r in rows if is_simple_scope(r["fields"])]
    return rows


def evaluate(gt_rows: list[dict], pred_rows: list[dict],
             mvp_only: bool = True) -> dict[str, object]:
    """칸별 정확도와 칸 평균(macro: 칸 정확도의 평균, micro: 채점한 셀 전체의 정답 비율)을 낸다.
    예측 쪽이 입력으로 받은 칸(meta.known_fields)과 상수 칸 accident_type은 평균에서 뺀다."""
    preds = {p["example_id"]: p for p in pred_rows}
    scored = 0
    missing_pred = 0
    given: set[str] = {"accident_type"}
    for p in pred_rows:
        given.update((p.get("meta") or {}).get("known_fields") or ())
    per_field: dict[str, dict] = defaultdict(lambda: {
        "n": 0, "correct": 0, "swapped_correct": 0, "swap_would_fix": 0, "pred_null": 0,
        "confusion": defaultdict(Counter), "by_confidence": defaultdict(lambda: [0, 0]),
    })
    for gt in gt_rows:
        if mvp_only and not gt.get("in_mvp_scope"):
            continue
        pred = preds.get(gt["example_id"])
        if pred is None:
            missing_pred += 1
            continue
        scored += 1
        for key, expected in gt["fields"].items():
            if expected is None:
                continue
            stat = per_field[key]
            value, confidence = _pred_value(pred, key)
            stat["n"] += 1
            stat["confusion"][str(expected)][str(value)] += 1
            if value is None:
                stat["pred_null"] += 1
            hit = value == expected
            if not hit and _swapped_match(gt["fields"], pred, key):
                # A/B를 맞바꾸면 맞는 건. 1인칭에서도 잦으면 촬영차=B 규약이 라벨에서 흔들린 것이다
                stat["swap_would_fix"] += 1
                if not gt.get("party_order_known", True):
                    hit = True
                    stat["swapped_correct"] += 1
            if hit:
                stat["correct"] += 1
            bucket = stat["by_confidence"][_bucket(confidence)]
            bucket[0] += 1
            bucket[1] += int(hit)
    fields = {}
    for key, stat in per_field.items():
        fields[key] = {
            "n": stat["n"],
            "accuracy": round(stat["correct"] / stat["n"], 4) if stat["n"] else None,
            "correct": stat["correct"],
            "swapped_correct": stat["swapped_correct"],
            "swap_would_fix": stat["swap_would_fix"],
            "pred_null_rate": round(stat["pred_null"] / stat["n"], 4) if stat["n"] else None,
            "confusion": {g: dict(c) for g, c in stat["confusion"].items()},
            "by_confidence": {
                b: {"n": n, "accuracy": round(c / n, 4)}
                for b, (n, c) in stat["by_confidence"].items()
            },
        }
    averaged = [k for k in fields if k not in given and fields[k]["n"]]
    macro = sum(fields[k]["accuracy"] for k in averaged) / len(averaged) if averaged else 0.0
    cells = sum(fields[k]["n"] for k in averaged)
    micro = sum(fields[k]["correct"] for k in averaged) / cells if cells else 0.0
    return {"scored_examples": scored, "missing_predictions": missing_pred, "mvp_only": mvp_only,
            "field_mean": {"fields": averaged, "macro_accuracy": round(macro, 4),
                           "micro_accuracy": round(micro, 4), "given_fields": sorted(given)},
            "fields": fields}


def format_table(report: dict[str, object]) -> str:
    lines = [f"scored={report['scored_examples']} missing_pred={report['missing_predictions']}",
             f"{'field':<20}{'n':>5}{'acc':>8}{'null':>8}{'swap':>6}{'swapfix':>9}"]
    for key, f in report["fields"].items():  # type: ignore[union-attr]
        acc = "-" if f["accuracy"] is None else f"{f['accuracy']:.2f}"
        null = "-" if f["pred_null_rate"] is None else f"{f['pred_null_rate']:.2f}"
        lines.append(f"{key:<20}{f['n']:>5}{acc:>8}{null:>8}{f['swapped_correct']:>6}"
                     f"{f['swap_would_fix']:>9}")
    mean = report["field_mean"]  # type: ignore[index]
    lines.append(f"field mean over {len(mean['fields'])} fields: "
                 f"macro={mean['macro_accuracy']:.3f} micro={mean['micro_accuracy']:.3f} "
                 f"(given: {', '.join(mean['given_fields'])})")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gt", type=Path, required=True)
    parser.add_argument("--pred", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--all-scope", action="store_true", help="MVP 범위 밖 사건도 채점")
    parser.add_argument("--min-sharpness", type=float, default=None,
                        help="화질 게이트. 이 값 미만(video_quality.sharpness)은 채점에서 뺀다")
    parser.add_argument("--simple-scope", action="store_true",
                        help="유턴·우회전끼리 유형을 뺀다")
    args = parser.parse_args()
    gt_rows = select_rows(read_jsonl(args.gt), mvp_only=not args.all_scope,
                          min_sharpness=args.min_sharpness, simple_scope=args.simple_scope)
    report = evaluate(gt_rows, read_jsonl(args.pred), mvp_only=not args.all_scope)
    report["filters"] = {"min_sharpness": args.min_sharpness, "simple_scope": args.simple_scope,
                         "gt_rows": len(gt_rows)}
    print(format_table(report))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1))
        print(f"-> {args.out}")


if __name__ == "__main__":
    main()
