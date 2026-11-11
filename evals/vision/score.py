"""정답 카드(엑셀)와 생성된 사실 카드를 칸별로 비교해 점수를 낸다. 정답 파일은 읽기만 한다.

사용:
    .venv/Scripts/python evals/vision/score.py evals/results/vision/runs/<run_id>

- 채점 칸: 선택(○) 칸과 narrative를 뺀 칸 (workers/vision/fields.py의 SCORED)
- 정답이 빈 칸은 아직 안 채운 것으로 보고 건너뛴다.
- collision_time_ms는 200ms(3프레임) 이내면 정답.
- 확인 표시 평가: 틀린 칸 중 needs_review로 잡은 비율(잡아냄), 맞은 칸 중 괜히 잡은 비율(헛표시).
- 대표 숫자는 핵심 칸(fields.CORE)만으로 낸다: 정답률, 표시 없는 핵심 칸의 정확도, 카드당 확인할
  핵심 칸.
  카드를 그대로 채점하고 담당자 수정은 가정하지 않는다(rescore_confirmed.py의 '장소 확인 후'는
  참고용).
- 핵심 8칸(fields.CORE): 개선 여부는 이 점수로 본다. '불명'으로 답한 칸은 따로 세어
  답한 비율(자동 처리 비율)과 답한 칸의 정확도를 낸다. 표에서는 ★로 표시하고 위에 둔다.
- 정답표에 없는 영상(예: 3인칭에서 바꾼 영상의 옛 결과)은 건너뛴다.
결과: <run_dir>/score.md, score.csv
"""

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from workers.vision.fields import (  # noqa: E402
    CORE,
    INTERSECTIONS,
    SCORED,
    SIGNAL_PLACES,
    STAFF_FIELDS,
    VISION_CORE,
    frame_to_ms,
    same_value,
)


def load_answers(path):
    from openpyxl import load_workbook  # 비전 환경(.venv)에만 있음

    ws = load_workbook(path, read_only=True, data_only=True)["정답"]
    rows = list(ws.iter_rows(values_only=True))
    header = list(rows[0])
    answers = {}
    for row in rows[2:]:
        if not row or not row[0]:
            continue
        rec = dict(zip(header, row, strict=False))
        frame = rec.get("collision_frame")
        rec["collision_time_ms"] = (
            frame_to_ms(int(frame)) if isinstance(frame, (int, float)) else None
        )
        answers[rec["video_name"]] = rec
    return answers


def coarse_part(part) -> str:
    """앞왼쪽·앞오른쪽 → 앞, 뒤왼쪽·뒤오른쪽 → 뒤. 왼쪽·오른쪽·불명은 그대로."""
    part = str(part or "")
    return "앞" if part.startswith("앞") else "뒤" if part.startswith("뒤") else part


def pct(a, b):
    return f"{a / b * 100:.0f}% ({a}/{b})" if b else "-"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument(
        "--answers", default=ROOT / "evals/vision/answers/answer_cards.xlsx", type=Path
    )
    args = parser.parse_args()
    answers = load_answers(args.answers)

    per_field = defaultdict(lambda: [0, 0])
    per_view = defaultdict(lambda: [0, 0])
    per_scope = defaultdict(lambda: [0, 0])
    caught = missed = false_flag = right_clean = 0
    coarse = [0, 0]  # 충돌 부위를 앞·뒤·왼쪽·오른쪽으로만 묶어 본 정답률
    detail_rows = []
    for path in sorted(args.run_dir.glob("*.json")):
        if path.name == "run.json":
            continue
        card = json.loads(path.read_text(encoding="utf-8"))
        ans = answers.get(card["case_id"])
        if not ans:
            continue
        view = ans.get("시점", "")
        # MVP 범위(교차로 사고) 여부는 정답 카드의 장소로 정한다
        cross = "교차로" if str(ans.get("location_type")) in INTERSECTIONS else "교차로 아님"
        for name in SCORED:
            truth = ans.get(name)
            if truth in (None, ""):
                continue
            got = card["fields"][name]["value"]
            if name.startswith("signal_") and str(ans.get("location_type")) not in SIGNAL_PLACES:
                continue  # 신호는 신호 교차로에서만 도표에 쓰인다(fields.tidy_by_location 주석)
            ok = same_value(
                name,
                got,
                truth
                if name == "collision_time_ms"
                else str(truth)
                if not isinstance(truth, int)
                else truth,
            )
            flagged = card["fields"][name]["verification_status"] == "needs_review"
            per_field[name][0] += ok
            per_field[name][1] += 1
            per_view[view][0] += ok
            per_view[view][1] += 1
            per_scope[cross][0] += ok
            per_scope[cross][1] += 1
            if ok:
                false_flag += flagged
                right_clean += not flagged
            else:
                caught += flagged
                missed += not flagged
            if name.startswith("collision_part_"):
                coarse[0] += coarse_part(got) == coarse_part(str(truth))
                coarse[1] += 1
            detail_rows.append(
                {
                    "video": card["case_id"],
                    "시점": view,
                    "field": name,
                    "truth": truth,
                    "got": got,
                    "correct": ok,
                    "needs_review": flagged,
                    "confidence": card["fields"][name]["model_confidence"],
                }
            )

    total_ok = sum(v[0] for v in per_field.values())
    total = sum(v[1] for v in per_field.values())
    # 찍기 기준: 칸마다 '이 영상들에서 가장 흔한 정답' 하나만 냈을 때의 점수.
    #  모델 점수가 이보다 높아야 영상을 보고 맞힌 것으로 볼 수 있다(collision_time_ms는 값이
    # 제각각이라 제외).
    majority = {}
    for name in SCORED:
        truths = [str(r["truth"]) for r in detail_rows if r["field"] == name]
        if truths and name != "collision_time_ms":
            majority[name] = (
                Counter(truths).most_common(1)[0][1],
                len(truths),
                Counter(truths).most_common(1)[0][0],
            )
    base_ok = sum(m[0] for m in majority.values())
    base_n = sum(m[1] for m in majority.values())
    model_ok_same = sum(per_field[n][0] for n in majority)

    # 핵심 8칸: 정답률, 찍기 기준, 그리고 '불명'을 뺀 정확도와 답한 비율(자동 처리 비율)
    core_rows = [r for r in detail_rows if r["field"] in CORE]
    core_ok = sum(r["correct"] for r in core_rows)
    core_base = sum(majority[n][0] for n in CORE if n in majority)
    answered = [r for r in core_rows if r["got"] not in ("불명", None)]
    videos = len({r["video"] for r in detail_rows})
    # 담당자 관점: 표시 없는 칸은 안 본다 → 그 칸을 믿을 수 있나, 카드마다 몇 칸을 봐야 하나
    clean = [r for r in detail_rows if not r["needs_review"]]
    flagged_n = len(detail_rows) - len(clean)

    def group(fields):
        rs = [r for r in detail_rows if r["field"] in fields]
        cl = [r for r in rs if not r["needs_review"]]
        return (
            pct(sum(r["correct"] for r in rs), len(rs)),
            pct(sum(r["correct"] for r in cl), len(cl)),
            (len(rs) - len(cl)) / max(videos, 1),
        )

    vis, staff = group(VISION_CORE), group(STAFF_FIELDS)
    #  대표 숫자: 핵심 칸(도표를 가르는 칸)만. 쉬운 칸(사고 대상·차량 수·차로 변경 등 거의 고정값)이
    # 섞이면 부풀려진다.
    core_clean = [r for r in core_rows if not r["needs_review"]]
    core_wrong = [r for r in core_rows if not r["correct"]]
    lines = [
        f"# 채점: {args.run_dir.name}",
        "",
        f"- 채점한 영상: {videos}건 (이 폴더 카드 그대로 채점. 담당자 수정을 가정하지 않음)",
        "",
        "## 대표 숫자 (핵심 칸: 장소·도로 관계·신호 A/B·움직임 A/B·상대 위치·진입 순서)",
        f"- **핵심 칸 정답률: {pct(core_ok, len(core_rows))}** (찍기 기준 "
        f"{pct(core_base, len(core_rows))})",
        f"- **표시 없는 핵심 칸의 정확도(담당자가 안 보는 칸): "
        f"{pct(sum(r['correct'] for r in core_clean), len(core_clean))}**",
        f"- **카드당 확인할 핵심 칸: {(len(core_rows) - len(core_clean)) / max(videos, 1):.1f}칸**",
        f"- 틀린 핵심 칸 중 확인 표시로 잡음: "
        f"{pct(sum(r['needs_review'] for r in core_wrong), len(core_wrong))}",
        f"- 비전 핵심(신호 A/B, 움직임 A/B, 상대 위치): {vis[0]} / 표시 없는 칸 정확도 {vis[1]}",
        f"- 담당자 칸(장소·도로 관계·진입 순서): {staff[0]} / 표시 없는 칸 정확도 {staff[1]}",
        "- 신호 칸은 신호 교차로(사거리_신호있음) 영상에서만 채점(다른 장소의 유형표에는 신호가 "
        "없음)",
        "",
        "## 참고 (쉬운 칸 포함 전체 칸)",
        f"- 핵심 칸 중 답한 칸('불명' 아님): {pct(len(answered), len(core_rows))}, "
        f"답한 칸의 정확도: {pct(sum(r['correct'] for r in answered), len(answered))}",
        f"- 표시 없는 칸의 정확도(전체 칸): {pct(sum(r['correct'] for r in clean), len(clean))}",
        f"- 카드당 확인할 칸(전체 칸): {flagged_n / videos if videos else 0:.1f}칸 (채점 칸 "
        f"{len(detail_rows) // max(videos, 1)}칸 중)",
        f"- 전체 정답률: {pct(total_ok, total)}",
        f"- 찍기 기준과 비교(충돌 순간 제외): 모델 {pct(model_ok_same, base_n)} vs 가장 흔한 답만 "
        f"냈을 때 {pct(base_ok, base_n)}",
        *[f"- {v}: {pct(*per_view[v])}" for v in sorted(per_view)],
        *[
            f"- {s} 영상(MVP 범위{'' if s == '교차로' else ' 밖'}): {pct(*per_scope[s])}"
            for s in sorted(per_scope)
        ],
        f"- 틀린 칸 중 확인 표시로 잡아냄: {pct(caught, caught + missed)}",
        f"- 맞은 칸 중 괜히 확인 표시: {pct(false_flag, false_flag + right_clean)}",
        f"- 충돌 부위 대략(앞·뒤·왼쪽·오른쪽으로 묶음): {pct(*coarse)}",
        "",
        "| 칸 | 정답률 | 찍기 기준 (가장 흔한 정답) | 판정 |",
        "| --- | --- | --- | --- |",
    ]
    for n in (
        VISION_CORE + STAFF_FIELDS + [n for n in SCORED if n not in CORE]
    ):  # 비전 핵심 ★, 담당자 칸 ☆
        if not per_field[n][1]:
            continue
        label = f"★ {n}" if n in VISION_CORE else f"☆ {n}" if n in STAFF_FIELDS else n
        if n in majority:
            ok_n, cnt, top = majority[n]
            diff = per_field[n][0] - ok_n
            mark = "찍기보다 나음" if diff > 0 else "찍기와 같음" if diff == 0 else "찍기보다 못함"
            lines.append(
                f"| {label} | {pct(*per_field[n])} | {pct(ok_n, cnt)} ('{top}') | {mark} |"
            )
        else:
            lines.append(f"| {label} | {pct(*per_field[n])} | - | - |")
    (args.run_dir / "score.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with (args.run_dir / "score.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(detail_rows[0]) if detail_rows else ["video"])
        w.writeheader()
        w.writerows(detail_rows)
    print("\n".join(lines))
    if not total:
        print("\n채점할 정답이 아직 없습니다(엑셀 칸이 비어 있음).")


if __name__ == "__main__":
    main()
