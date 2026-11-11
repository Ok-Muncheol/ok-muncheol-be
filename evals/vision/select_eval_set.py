"""평가 영상 30건(개선용 10 + 최종 평가용 20)을 고른다.

사용:
    python evals/vision/select_eval_set.py
    python evals/vision/select_eval_set.py --labels evals/results/vision/labels.csv --seed 20261105

입력은 scan_labels.py가 만든 labels.csv다. 결과는 evals/results/vision/에 저장한다.
- eval_set.csv          고른 30건과 split(dev/test)
- eval_set_criteria.md  고른 기준·후보 수·장소별 몫·분포
최종 평가용(test) 20건은 프롬프트나 코드를 고치는 데 쓰지 않는다.
"""

import argparse
import csv
import math
import random
from collections import Counter, defaultdict
from pathlib import Path

TOTAL = 30
DEV = 10
MIN_PER_PLACE = 2
THIRD_PERSON_SHARE = 0.25
COLLISION_WINDOW = (60, 90)  # 충돌(약 75번 프레임) 전후 1초
ALLOWED_OBJECTS = {"차대차", "고속도로"}
# 0번: 선행자동차(1차사고차량)를 추돌 → 3대 이상 연쇄 사고로 보여 서비스 범위 밖
EXCLUDED_TYPES = {"0"}


def frames(row):
    return [int(f) for f in row["ab_frames"].split(";") if f]


def has_boxes(row):
    a, b = int(row["a_boxes"]) > 0, int(row["b_boxes"]) > 0
    # 1인칭은 내 차(B)가 화면에 없을 수 있어 A나 B 중 하나만 있어도 된다
    return (a or b) if row["video_point_of_view"] == "1" else (a and b)


def reject_reason(row):
    """후보에서 빼는 이유. 빼지 않으면 None."""
    if row["map_사고대상"] not in ALLOWED_OBJECTS:
        return "차대차·고속도로 아님(유형표에 없음 포함)"
    if row["accident_type"] in EXCLUDED_TYPES:
        return "연쇄 사고 유형(0번) 제외"
    if row["has_mp4"] != "True":
        return "mp4 없음"
    if row["accidental_negligence_rateA"] == "" and row["map_기본과실A"] == "":
        return "과실비율 없음"
    if not has_boxes(row):
        return "정답 박스 조건 미달"
    lo, hi = COLLISION_WINDOW
    if not any(lo <= f <= hi for f in frames(row)):
        return f"{lo}~{hi}번 프레임에 A/B 박스 없음"
    return None


def place_quotas(counts):
    """장소별 몫: 영상 수의 제곱근 비율, 장소마다 최소 MIN_PER_PLACE, 합계 TOTAL."""
    weights = {p: math.sqrt(n) for p, n in counts.items()}
    total_w = sum(weights.values())
    raw = {p: TOTAL * w / total_w for p, w in weights.items()}
    quota = {p: min(counts[p], max(MIN_PER_PLACE, math.floor(r))) for p, r in raw.items()}
    # 남은 몫은 소수점 아래가 큰 장소부터, 많으면 몫이 큰 장소부터 줄인다
    while sum(quota.values()) < TOTAL:
        p = max((p for p in quota if quota[p] < counts[p]), key=lambda p: raw[p] - quota[p])
        quota[p] += 1
    while sum(quota.values()) > TOTAL:
        p = max((p for p in quota if quota[p] > MIN_PER_PLACE), key=lambda p: quota[p] - raw[p])
        quota[p] -= 1
    return quota


def pick_diverse(rows, n, rng):
    """사고 유형이 겹치지 않게 n건을 고른다. 유형을 다 쓰면 다시 돈다."""
    by_type = defaultdict(list)
    for r in rows:
        by_type[r["accident_type"]].append(r)
    for lst in by_type.values():
        rng.shuffle(lst)
    types = sorted(by_type)
    rng.shuffle(types)
    picked = []
    while len(picked) < n and any(by_type.values()):
        for t in types:
            if by_type[t] and len(picked) < n:
                picked.append(by_type[t].pop())
    return picked


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--labels", default=Path("evals/results/vision/labels.csv"), type=Path)
    parser.add_argument("--out-dir", default=Path("evals/results/vision"), type=Path)
    parser.add_argument("--seed", default=20261105, type=int)
    args = parser.parse_args()
    rng = random.Random(args.seed)

    with args.labels.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    reasons = Counter()
    candidates = []
    for r in rows:
        why = reject_reason(r)
        if why:
            reasons[why] += 1
        else:
            candidates.append(r)

    by_place = defaultdict(list)
    for r in candidates:
        by_place[r["map_사고장소"]].append(r)
    quota = place_quotas({p: len(v) for p, v in by_place.items()})

    selected = []
    for place in sorted(by_place):
        pool = by_place[place]
        third = [r for r in pool if r["video_point_of_view"] == "3"]
        first = [r for r in pool if r["video_point_of_view"] == "1"]
        n3 = min(len(third), round(quota[place] * THIRD_PERSON_SHARE))
        part = pick_diverse(third, n3, rng)
        part += pick_diverse(first, quota[place] - len(part), rng)
        if len(part) < quota[place]:  # 1인칭이 모자라면 3인칭으로 채운다
            rest = [r for r in third if r not in part]
            part += pick_diverse(rest, quota[place] - len(part), rng)
        selected += part

    # 개선용(dev): 장소마다 1건씩, 남는 몫은 몫이 큰 장소부터 1건씩 더
    for r in selected:
        r["split"] = "test"
    groups = defaultdict(list)
    for r in selected:
        groups[r["map_사고장소"]].append(r)
    order = sorted(groups, key=lambda p: (-quota[p], p))  # 몫이 큰 장소부터
    dev_count = 0
    for place in order * 3:  # 1바퀴째 모든 장소, 그다음은 큰 장소부터
        if dev_count == DEV:
            break
        rest = [r for r in groups[place] if r["split"] == "test"]
        if len(rest) > 1:  # 장소마다 test를 최소 1건 남긴다
            rng.choice(rest)["split"] = "dev"
            dev_count += 1
    selected.sort(key=lambda r: (r["map_사고장소"], r["video_point_of_view"], r["video_name"]))

    out_cols = [
        "split",
        "video_name",
        "map_사고장소",
        "video_point_of_view",
        "accident_type",
        "map_장소특징",
        "map_A진행",
        "map_B진행",
        "map_A신호",
        "map_B신호",
        "accidental_negligence_rateA",
        "accidental_negligence_rateB",
        "map_기본과실A",
        "map_기본과실B",
        "filming_way",
        "a_boxes",
        "b_boxes",
        "ab_frames",
        "mp4_path",
    ]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = args.out_dir / "eval_set.csv"
    with out_csv.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=out_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(selected)

    def table(counter, head):
        lines = [f"| {head} | 수 |", "| --- | --- |"]
        lines += [f"| {k} | {v} |" for k, v in sorted(counter.items())]
        return "\n".join(lines)

    lo, hi = COLLISION_WINDOW
    md = [
        "# 평가 영상 30건 선정 기준",
        "",
        f"- 입력: `{args.labels.as_posix()}` ({len(rows)}건)",
        f"- 시드: {args.seed} (같은 입력·시드면 같은 결과)",
        "- 최종 평가용(test) 20건은 프롬프트·코드 개선에 쓰지 않는다.",
        "",
        "## 후보 조건",
        f"1. 유형표 사고대상이 {', '.join(sorted(ALLOWED_OBJECTS))}. "
        f"유형 {', '.join(sorted(EXCLUDED_TYPES))}번(연쇄 사고로 보임)은 제외",
        "2. mp4가 있음",
        "3. 과실비율이 라벨이나 유형표 기본과실에 있음",
        "4. 정답 박스: 1인칭은 A나 B, 3인칭은 A와 B 모두",
        f"5. 충돌 전후 {lo}~{hi}번 프레임에 A/B 박스가 하나 이상",
        "",
        f"후보 {len(candidates)}건. 제외 이유:",
        "",
        table(reasons, "제외 이유"),
        "",
        "## 고르는 방법",
        f"1. 장소별 몫: 후보 수의 제곱근 비율, 장소마다 최소 {MIN_PER_PLACE}건, 합계 {TOTAL}건",
        f"2. 장소마다 약 {int(THIRD_PERSON_SHARE * 100)}%를 3인칭으로 (3인칭이 없으면 1인칭만)",
        "3. 장소 안에서 사고 유형이 겹치지 않게 고름 (유형을 다 쓴 뒤에만 반복)",
        f"4. 개선용(dev) {DEV}건: 장소마다 1건씩, 남는 몫은 몫이 큰 장소부터 1건씩. 나머지는 최종 평가용(test)",
        "",
        "## 장소별 후보 수와 몫",
        "",
        "| 장소 | 후보 | 몫 |",
        "| --- | --- | --- |",
        *[f"| {p} | {len(by_place[p])} | {quota[p]} |" for p in sorted(by_place)],
        "",
        "## 결과 분포",
        "",
        table(Counter(r["split"] for r in selected), "split"),
        "",
        table(
            Counter(f"{r['video_point_of_view']}인칭 / {r['split']}" for r in selected),
            "시점 / split",
        ),
        "",
        table(Counter(f"{r['map_사고장소']} / {r['split']}" for r in selected), "장소 / split"),
        "",
        f"- 사고 유형 종류: {len({r['accident_type'] for r in selected})}개 / {len(selected)}건",
        f"- 신호 정답(A신호·B신호)이 있는 영상: {sum(bool(r['map_A신호'] or r['map_B신호']) for r in selected)}건",
        "",
        "## 선정 목록",
        "",
        "| split | 영상 | 장소 | 시점 | 유형 | A진행 | B진행 | 과실 A:B |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in selected:
        a = r["accidental_negligence_rateA"] or r["map_기본과실A"]
        b = r["accidental_negligence_rateB"] or r["map_기본과실B"]
        md.append(
            f"| {r['split']} | {r['video_name']} | {r['map_사고장소']} | {r['video_point_of_view']} | "
            f"{r['accident_type']} | {r['map_A진행']} | {r['map_B진행']} | {a}:{b} |"
        )
    out_md = args.out_dir / "eval_set_criteria.md"
    out_md.write_text("\n".join(md) + "\n", encoding="utf-8")

    print(
        f"후보 {len(candidates)}건 → 선정 {len(selected)}건 "
        f"(dev {sum(r['split'] == 'dev' for r in selected)}, test {sum(r['split'] == 'test' for r in selected)})"
    )
    print("저장:", out_csv, out_md)


if __name__ == "__main__":
    main()
