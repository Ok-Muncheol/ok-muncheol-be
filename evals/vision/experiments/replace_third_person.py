"""평가 영상 30건 중 3인칭을 같은 장소·같은 구분(dev/test)의 1인칭으로 바꾼다. 나머지는 그대로 둔다.

이유: MVP를 '자차 블랙박스(1인칭)만 처리'로 정했다(2026-11-09). 3인칭은 A/B가 정해져 있지 않아
코드 판단(카메라 계산, 박스 판단)을 쓸 수 없다. 이미 만든 정답 카드를 지키려고 30건 전체를 다시 고르지 않는다.

사용:
    .venv/Scripts/python evals/vision/experiments/replace_third_person.py           # 미리보기(파일 안 바꿈)
    .venv/Scripts/python evals/vision/experiments/replace_third_person.py --apply   # 적용

고르는 규칙(select_eval_set.py와 같은 후보 조건 reject_reason 사용):
1. 같은 장소, 1인칭, 아직 평가 목록에 없는 영상
2. 그 장소에서 아직 쓰지 않은 사고 유형을 먼저. 없으면 후보 전체에서
3. 시드 고정(같은 입력·시드면 같은 결과)

--apply 하면:
- 원래 eval_set.csv를 backup/eval_set_before_1st_person.csv로 복사(이미 있으면 덮어쓰지 않음)
- eval_set.csv에서 6건을 바꿈(바뀐 줄 순서는 select_eval_set.py와 같은 정렬)
- eval_set_added.csv: 새로 들어온 영상만(영상 꺼내기·정답 입력표 만들 때 씀)
- eval_set_criteria.md 끝에 바꾼 내역을 덧붙임
"""

import argparse
import csv
import random
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # evals/vision 공통 도구
sys.path.insert(0, str(Path(__file__).resolve().parent))  # 이 폴더의 실험 모듈
from select_eval_set import reject_reason  # noqa: E402

RESULTS = Path("evals/results/vision")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval-set", default=RESULTS / "eval_set.csv", type=Path)
    parser.add_argument("--labels", default=RESULTS / "labels.csv", type=Path)
    parser.add_argument("--seed", default=20261109, type=int)
    parser.add_argument("--apply", action="store_true", help="붙이지 않으면 미리보기만 한다")
    args = parser.parse_args()
    rng = random.Random(args.seed)

    with args.eval_set.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames
        rows = list(reader)
    with args.labels.open(encoding="utf-8-sig") as f:
        labels = list(csv.DictReader(f))

    chosen = {r["video_name"] for r in rows}
    third = sorted((r for r in rows if r["video_point_of_view"] == "3"), key=lambda r: r["video_name"])
    if not third:
        raise SystemExit("3인칭 영상이 없습니다. 이미 바꾼 것 같습니다.")

    swaps = []  # (빠지는 줄, 들어오는 줄)
    for old in third:
        place = old["map_사고장소"]
        pool = sorted((x for x in labels if x["map_사고장소"] == place and x["video_point_of_view"] == "1"
                       and x["video_name"] not in chosen and reject_reason(x) is None),
                      key=lambda x: x["video_name"])
        if not pool:
            raise SystemExit(f"{place}: 바꿀 1인칭 후보가 없습니다.")
        used = {r["accident_type"] for r in rows if r["map_사고장소"] == place}
        used |= {new["accident_type"] for _, new in swaps if new["map_사고장소"] == place}
        fresh = [x for x in pool if x["accident_type"] not in used]
        new = dict(rng.choice(fresh or pool))
        new["split"] = old["split"]
        chosen.add(new["video_name"])
        swaps.append((old, new))

    print(f"3인칭 {len(third)}건 → 1인칭으로 바꿈 (시드 {args.seed})\n")
    for old, new in swaps:
        print(f"[{old['split']}] {old['map_사고장소']}")
        print(f"   빠짐  {old['video_name']}  유형 {old['accident_type']}: {old['map_A진행']} / {old['map_B진행']}")
        print(f"   들어옴 {new['video_name']}  유형 {new['accident_type']}: {new['map_A진행']} / {new['map_B진행']}")

    if not args.apply:
        print("\n미리보기입니다. 적용하려면 --apply를 붙이세요.")
        return

    backup = RESULTS / "backup" / "eval_set_before_1st_person.csv"
    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.exists():
        shutil.copy2(args.eval_set, backup)

    gone = {old["video_name"] for old, _ in swaps}
    out_rows = [r for r in rows if r["video_name"] not in gone] + [new for _, new in swaps]
    out_rows.sort(key=lambda r: (r["map_사고장소"], r["video_point_of_view"], r["video_name"]))

    def write(path, data):
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(data)

    write(args.eval_set, out_rows)
    write(RESULTS / "eval_set_added.csv", [new for _, new in swaps])

    md = ["", "## 3인칭 → 1인칭 교체 (2026-11-09)", "",
          "MVP를 자차 블랙박스(1인칭)만 처리하기로 정해서, 3인칭을 같은 장소·같은 구분의 1인칭으로 바꿨다.",
          f"`replace_third_person.py`, 시드 {args.seed}. 원래 목록은 `backup/eval_set_before_1st_person.csv`.", "",
          "| 구분 | 장소 | 빠짐 (유형) | 들어옴 (유형) |", "| --- | --- | --- | --- |"]
    md += [f"| {o['split']} | {o['map_사고장소']} | {o['video_name']} ({o['accident_type']}) | "
           f"{n['video_name']} ({n['accident_type']}) |" for o, n in swaps]
    with (RESULTS / "eval_set_criteria.md").open("a", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")

    print(f"\n적용함: {args.eval_set} ({len(out_rows)}건), {RESULTS / 'eval_set_added.csv'} ({len(swaps)}건)")
    print(f"원본 백업: {backup}")


if __name__ == "__main__":
    main()
