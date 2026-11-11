"""사람 라벨 64건(구준휘님 라벨 도구, 영상만 보고 붙임)으로 움직임 A·B를 잰다. 무료.

사용:
    .venv/Scripts/python evals/vision/experiments/eval_human64.py

- 라벨: evals/results/vision/human_labels_jh_2026-11-09.jsonl (원격 main의 evals/vision/labels/에서
  복사,
  status=scored만)
  movement_ego(촬영 차량) → 움직임 B, movement_other(상대 차) → 움직임 A. 우리 카드도 촬영 차량=B라
  그대로 맞음
- 이 78건은 우리 분류기 학습 1,256건에 들어 있으므로, **학습에서 빼고 다시 학습(메모리에서만, 저장
  안 함)** 한 뒤 잰다
- 장소: AI Hub 라벨 장소(데이터 폴더 기준, 담당자가 확인한 장소와 같다고 봄)
비교: 카메라 규칙(B), 전부 직진(A), AI Hub 라벨 자체가 사람 라벨과 맞는 비율, 구준휘님 보고서의 VLM
숫자(같은 64건)
"""

import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # evals/vision 공통 도구
sys.path.insert(0, str(Path(__file__).resolve().parent))  # 이 폴더의 실험 모듈
from silver import ego_cached  # noqa: E402
from train_motion import fit_motion, video_features  # noqa: E402

import workers.vision.motion_model as mm  # noqa: E402
from workers.vision.scene_embed import embed_video  # noqa: E402

RESULTS = ROOT / "evals/results/vision"
VIDEOS = ROOT / "data/private/aihub/videos"
LABELS = RESULTS / "human_labels_jh_2026-11-09.jsonl"
EN = {"straight": "직진", "left_turn": "좌회전", "right_turn": "우회전", "u_turn": "유턴"}
FOUR = ("직진", "좌회전", "우회전")


def four(v):
    return v if v in FOUR else "기타"


def main():
    labels = [json.loads(line) for line in LABELS.open(encoding="utf-8")]
    held_out = {r["example_id"] for r in labels}
    human = {
        r["example_id"]: {
            "movement_B": EN.get(r["movement_ego"], r["movement_ego"]),
            "movement_A": EN.get(r["movement_other"], r["movement_other"]),
        }
        for r in labels
        if r["status"] == "scored"
    }
    with (RESULTS / "train_answers.csv").open(encoding="utf-8-sig") as f:
        answers = {r["video_name"]: r for r in csv.DictReader(f)}
    #  사람 라벨 영상 중 평가 30건에 든 것도 있어 장소·AI Hub 값은 전체 라벨 목록에서 만든다(같은
    # 규칙)
    from make_answer_sheet import prefill

    with (RESULTS / "labels.csv").open(encoding="utf-8-sig") as f:
        aihub = {
            r["video_name"]: prefill(r) for r in csv.DictReader(f) if r["video_name"] in held_out
        }

    feats = {n: x for n in answers if n not in held_out and (x := video_features(n)) is not None}
    columns = sorted({k for x in feats.values() for k in x})
    print(f"학습: {len(feats)}건 (사람 라벨 {len(held_out)}건 뺌)")
    mm._BUNDLES.clear()
    mm._BUNDLES.update(fit_motion(feats, answers, columns, verbose=False))
    places = mm._BUNDLES["movement_A"]["places"]
    b_classes = mm._BUNDLES["movement_A"]["b_classes"]

    n = 0
    ok = Counter()
    conf_b, conf_a = Counter(), Counter()
    for name, truth in human.items():
        x = video_features(name)
        if x is None:
            continue
        n += 1
        place = aihub[name].get("location_type")
        emb = embed_video(str(VIDEOS / f"{name}.mp4"), name)
        px = mm.place_onehot(place, places)
        pb = mm.predict("movement_B", x, emb, px)
        pa = mm.predict("movement_A", x, emb, px + [pb["all"].get(c, 0.0) for c in b_classes])
        rule_b = ego_cached(name).field_opinions(place)["movement_B"]["value"]
        tb, ta = truth["movement_B"], truth["movement_A"]
        # 카드에서처럼: 분류기가 기타면 B는 카메라 규칙, A는 (VLM 대신) 직진으로 둔다
        card_b = pb["value"] if pb["value"] != "기타" else rule_b
        card_a = pa["value"] if pa["value"] != "기타" else "직진"
        ok["B 분류기"] += card_b == tb
        ok["B 카메라 규칙"] += rule_b == tb
        ok["B 전부 직진"] += tb == "직진"
        ok["B AI Hub 라벨"] += aihub[name].get("movement_B") == tb
        ok["A 분류기"] += card_a == ta
        ok["A 전부 직진"] += ta == "직진"
        ok["A AI Hub 라벨"] += aihub[name].get("movement_A") == ta
        conf_b[(tb, card_b)] += 1
        conf_a[(ta, card_a)] += 1
    print(f"\n사람 라벨 {n}건 (장소는 확인된 장소)")
    for k in (
        "B 분류기",
        "B 카메라 규칙",
        "B 전부 직진",
        "B AI Hub 라벨",
        "A 분류기",
        "A 전부 직진",
        "A AI Hub 라벨",
    ):
        print(f"  {k:14} {ok[k]}/{n} ({ok[k] / n * 100:.0f}%)")
    print("  (구준휘님 보고서, 같은 64건 VLM: B 0.67, A 0.38~0.50)")
    for label, conf in (("움직임 B", conf_b), ("움직임 A", conf_a)):
        print(f"\n{label} 정답 → 분류기(카드 값):")
        for (t, g), c in conf.most_common(10):
            print(f"  {t} → {g}: {c}{'' if t == g else '  ✗'}")


if __name__ == "__main__":
    main()
