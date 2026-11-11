"""사실 카드 v1 형식(팀 계약, 영어 12칸 — 2026-11-11 확정)으로 다시 채점한다. 무료(저장된 결과만 읽음, 몇 초).

사용:
    .venv/Scripts/python evals/vision/score_v1.py                       # 기본: runs/dev30_1110_typeflag
    .venv/Scripts/python evals/vision/score_v1.py evals/results/vision/runs/<폴더>

- 30건: score.py가 만든 <폴더>/score.csv의 정답·카드 값을 v1 값으로 바꿔 다시 맞춰 본다(확인 표시는 그대로).
- 사람 92건(장소·움직임 A·B, 다시 본 라벨 반영): 지금 카드와 같은 규칙으로 계산(장소 = 영상 모델 v2,
  A = v2 + 진로변경·추월만 j3, B = 분류기 — video_human_preds.json 저장값). GPU로 1~2분.
- 마지막에 보고서 표: 장소·움직임은 92건, 나머지 칸은 30건 개발 세트. 서로 다른 세트라 평균을 내지 않는다.
'불명'은 v1에서 null. 정답·카드가 둘 다 null이면 지금 채점(score.py)처럼 맞은 것으로 센다.
"""

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "evals/results/vision"

LOCATION = {"사거리_신호있음": "signalized_intersection", "사거리_신호없음": "unsignalized_intersection",
            "T자형교차로": "unsignalized_intersection", "회전교차로": "roundabout", "직선도로": "straight_road",
            "고속도로": "highway", "주차장": "other", "차도와비차도": "other"}
ROAD = {"동일폭": "same_width", "A대로": "major_A", "B대로": "major_B", "A쪽_일시정지": "stop_A",
        "B쪽_일시정지": "stop_B", "A쪽만_신호등": "other", "B쪽만_신호등": "other", "해당없음": "none"}
SIGNAL = {"녹색": "green", "황색": "yellow", "적색": "red", "녹색좌회전": "green_left_arrow",
          "녹색직진좌회전": "green_left_arrow", "신호없음": "no_signal"}
MOVE = {"직진": "straight", "좌회전": "left_turn", "우회전": "right_turn", "유턴": "u_turn",
        "진로변경": "lane_change", "추월": "lane_change", "합류": "lane_change"}
POSITION = {"같은방향_앞": "same_direction_front", "같은방향_뒤": "same_direction_rear", "맞은편": "opposite",
            "왼쪽": "side", "오른쪽": "side"}
ENTRY = {"A먼저": "first", "B먼저": "second", "동시": "simultaneous", "해당없음": "none"}
MAPS = {"location_type": LOCATION, "road_priority": ROAD, "signal_A": SIGNAL, "signal_B": SIGNAL,
        "movement_A": MOVE, "movement_B": MOVE, "relative_position": POSITION, "entry_order": ENTRY}
CORE = list(MAPS)


def to_v1(field, value):
    """우리 카드 값 → v1 값. 불명·빈값 → None. 움직임의 목록 밖 값 → other."""
    if value in (None, "", "불명"):
        return None
    if field.startswith("movement_"):
        return MOVE.get(value, "other")
    return MAPS[field].get(value, value)


def main():
    run = Path(sys.argv[1]) if len(sys.argv) > 1 else RESULTS / "runs/dev30_1110_typeflag"
    rows = [r for r in csv.DictReader((run / "score.csv").open(encoding="utf-8-sig")) if r["field"] in MAPS]
    by = defaultdict(lambda: {"n": 0, "old": 0, "new": 0, "flag": 0, "clean_ok": 0, "clean": 0})
    for r in rows:
        f = by[r["field"]]
        ok = to_v1(r["field"], r["truth"]) == to_v1(r["field"], r["got"])
        flagged = r["needs_review"] == "True"
        f["n"] += 1
        f["old"] += r["correct"] == "True"
        f["new"] += ok
        f["flag"] += flagged
        if not flagged:
            f["clean"] += 1
            f["clean_ok"] += ok
    videos = len({r["video"] for r in rows})
    print(f"== {run.name}: v1 형식 핵심 칸 채점 ({videos}건)\n")
    print("| 칸 | 지금 형식 | v1 형식 | 확인 표시 | 표시 없는 칸 정확도 |")
    print("| --- | --- | --- | --- | --- |")
    tot = defaultdict(int)
    for name in CORE:
        f = by[name]
        if not f["n"]:
            continue
        clean = f"{f['clean_ok']}/{f['clean']}" if f["clean"] else "-"
        print(f"| {name} | {f['old']}/{f['n']} | **{f['new']}/{f['n']}** | {f['flag']} | {clean} |")
        for k, v in f.items():
            tot[k] += v
    print(f"\n핵심 칸 정답률: 지금 형식 {tot['old']}/{tot['n']} ({tot['old'] / tot['n']:.0%}) → "
          f"v1 형식 {tot['new']}/{tot['n']} ({tot['new'] / tot['n']:.0%})")
    print(f"표시 없는 핵심 칸 정확도: {tot['clean_ok']}/{tot['clean']} ({tot['clean_ok'] / max(tot['clean'], 1):.0%}), "
          f"카드당 확인할 핵심 칸 {tot['flag'] / videos:.1f}칸, 틀린 칸 중 표시로 잡음 "
          f"{(tot['n'] - tot['new']) - (tot['clean'] - tot['clean_ok'])}/{tot['n'] - tot['new']}")
    nos = [r for r in rows if r["field"] in ("signal_A", "signal_B")]
    if nos:
        t2 = tot["new"] - sum(by[s]["new"] for s in ("signal_A", "signal_B"))
        n2 = tot["n"] - sum(by[s]["n"] for s in ("signal_A", "signal_B"))
        print(f"신호 제외: {t2}/{n2} ({t2 / n2:.0%})")

    # ---- 사람 정답 92건: 장소·움직임 A·B ----
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from eval_video_human import load_human
    from workers.vision.video_model import CACHE, predict

    human = load_human()
    old = json.loads((RESULTS / "video_human_preds.json").read_text(encoding="utf-8"))
    names = sorted(n for n in human if (CACHE / f"{n}.npy").exists())

    def fix_a(v):
        return "직진" if v == "기타" else v

    # 지금 카드와 같은 규칙: 장소 = v2, B = 분류기(저장값), A = v2 + 진로변경·추월만 j3
    place, move_a, move_b = {}, {}, {}
    for n in names:
        v2 = predict("", n, spec="v2")
        j3 = fix_a(predict("", n, spec="j3")["movement_A"]["value"])
        place[n] = v2["location_type"]["value"]
        move_a[n] = j3 if j3 in ("진로변경", "추월") else fix_a(v2["movement_A"]["value"])
        move_b[n] = (old.get(n) or {}).get("classifier_B")
    human_acc = {}
    for field, pred in (("location_type", place), ("movement_A", move_a), ("movement_B", move_b)):
        sel = [n for n in names if human[n].get(field) and pred.get(n)]
        ok = sum(to_v1(field, pred[n]) == to_v1(field, human[n][field]) for n in sel)
        human_acc[field] = (ok, len(sel))

    # ---- 보고서 표: 칸마다 가장 좋은 정답으로 ----
    src = {"location_type": "92건 (사람 28 + AI Hub 라벨 64)", "movement_A": "92건 사람 정답",
           "movement_B": "92건 사람 정답"}
    print("\n== 보고서 표 (칸마다 가장 좋은 정답 세트, 서로 다른 세트라 평균을 내지 않는다)\n")
    print("| 칸 | 정확도 | 평가 세트 |")
    print("| --- | --- | --- |")
    for name in CORE:
        if name in human_acc:
            ok, n = human_acc[name]
            print(f"| {name} | **{ok / n:.0%}** ({ok}/{n}) | {src[name]} |")
        elif by[name]["n"]:
            f = by[name]
            print(f"| {name} | {f['new'] / f['n']:.0%} ({f['new']}/{f['n']}) | {videos}건 개발 세트(부풀려질 수 있음) |")
    print(f"\n카드 전체({videos}건 개발 세트): 핵심 칸 {tot['new'] / tot['n']:.0%}, "
          f"표시 없는 핵심 칸 정확도 {tot['clean_ok'] / max(tot['clean'], 1):.0%}, 카드당 확인할 핵심 칸 {tot['flag'] / videos:.1f}칸")


if __name__ == "__main__":
    main()
