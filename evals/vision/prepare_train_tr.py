"""AI Hub Training(약 1만 1천 건)으로 영상 모델 학습 데이터를 만든다. 무료(인터넷·API 안 씀).

사용:
    .venv/Scripts/python evals/vision/prepare_train_tr.py --max-per-place 1500 # 중간에 멈춰도
    이어서
    .venv/Scripts/python evals/vision/prepare_train_tr.py --tag all # 전체 11,275건(v4,
    2026-11-10)

- 대상: labels_train.csv(scan_labels.py로 만든 Training 라벨)의 1인칭 중
  select_eval_set.reject_reason 통과
  사람 정답 영상(평가 30건 + 구준휘님 78건)은 혹시 겹쳐도 뺀다(2026-11-10 확인: 겹침 0건)
  --max-per-place: 장소(유형표 기준)마다 최대 건수. 직선도로가 55%라 줄이면 학습 시간이 줄고 장소가
    고르게 된다
- 정답: make_answer_sheet.prefill(Validation과 같은 규칙, A/B 바꿔 읽기 포함) → train_answers_tr.csv
- 영상: zip에서 하나씩 임시 폴더로 꺼내 → video_model.load_clip과 같은 방법으로 16장 →
  clip16/<이름>.npy → mp4 바로 삭제
  원본 zip은 건드리지 않는다. 이미 npy가 있는 영상은 건너뛴다.
"""

import argparse
import csv
import json
import os
import random
import shutil
import sys
import time
import zipfile
from collections import Counter, defaultdict
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_answer_sheet import prefill  # noqa: E402
from select_eval_set import reject_reason  # noqa: E402

from workers.vision.video_model import CACHE, load_clip  # noqa: E402

RESULTS = ROOT / "evals/results/vision"
# 구준휘님 사람 라벨(scored 64건)
HUMAN_LABELS = ROOT / "evals/vision/labels/human_labels_2026-11-09.jsonl"
TMP = ROOT / "data/private/tmp_clip"  # 영어 경로(OpenCV가 한글 경로를 못 여는 경우가 있음)
FIELDS = [
    "location_type",
    "road_priority",
    "signal_A",
    "signal_B",
    "movement_A",
    "movement_B",
    "relative_position",
    "entry_order",
]

_ZIPS = {}


def _zip(path):
    if path not in _ZIPS:
        _ZIPS[path] = zipfile.ZipFile(path)
    return _ZIPS[path]


def make_clip(item):
    """(이름, mp4_path) → (이름, 결과 글자). 작업자 프로세스에서 돈다."""
    import numpy as np

    name, src = item
    out = CACHE / f"{name}.npy"
    if out.exists():
        return name, "skip"
    zpath, inner = src.split("::", 1)
    zpath = Path(zpath) if Path(zpath).is_absolute() else ROOT / zpath
    tmp = TMP / f"{name}.mp4"
    try:
        with _zip(zpath).open(inner) as fin, tmp.open("wb") as fout:
            shutil.copyfileobj(fin, fout)
        arr = load_clip(str(tmp))
        if not arr.any():
            return name, "empty"  # 한 장도 못 읽음 → 저장하지 않음
        part = (
            CACHE / f"{name}.part.npy"
        )  # 다 쓴 뒤에 이름을 바꿔서, 중간에 멈춰도 깨진 파일이 안 남게
        np.save(part, arr)
        os.replace(part, out)
        return name, "ok"
    except Exception as e:  # noqa: BLE001
        return name, f"error {type(e).__name__}: {e}"
    finally:
        tmp.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--labels", default=RESULTS / "labels_train.csv", type=Path)
    parser.add_argument("--max-per-place", default=0, type=int, help="0이면 전부")
    parser.add_argument("--workers", default=4, type=int)
    parser.add_argument("--seed", default=20261110, type=int)
    parser.add_argument(
        "--tag",
        default="tr",
        help="결과 표 이름: train_set_<tag>.csv, train_answers_<tag>.csv (2026-11-10 v4용 all, "
             "기존 tr은 안 덮음)",
    )
    parser.add_argument("--no-clips", action="store_true", help="표만 만들고 16장 뽑기는 하지 않음")
    args = parser.parse_args()

    with args.labels.open(encoding="utf-8-sig") as f:
        labels = list(csv.DictReader(f))
        cols = list(labels[0])
    with (RESULTS / "eval_set.csv").open(encoding="utf-8-sig") as f:
        human = {r["video_name"] for r in csv.DictReader(f)}
    human |= {
        json.loads(x)["example_id"]
        for x in HUMAN_LABELS.open(encoding="utf-8")
    }

    by_place = defaultdict(list)
    for r in labels:
        if (
            r["video_point_of_view"] == "1"
            and r["video_name"] not in human
            and reject_reason(r) is None
        ):
            by_place[r["map_사고장소"]].append(r)
    rng = random.Random(args.seed)
    rows = []
    for place in sorted(by_place):
        group = sorted(by_place[place], key=lambda r: r["video_name"])
        if args.max_per_place and len(group) > args.max_per_place:
            group = rng.sample(group, args.max_per_place)
        rows += group
    rows.sort(key=lambda r: r["video_name"])
    print(
        "장소별:", dict(Counter(r["map_사고장소"] for r in rows)), f"합계 {len(rows)}건", flush=True
    )

    with (RESULTS / f"train_set_{args.tag}.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=[*cols, "split"], extrasaction="ignore")
        w.writeheader()
        w.writerows({**r, "split": "train_tr"} for r in rows)
    with (RESULTS / f"train_answers_{args.tag}.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as f:
        w = csv.DictWriter(f, fieldnames=["video_name", "accident_type", *FIELDS])
        w.writeheader()
        for r in rows:
            pre = prefill(r)
            w.writerow(
                {
                    "video_name": r["video_name"],
                    "accident_type": r["accident_type"],
                    **{k: pre.get(k, "") for k in FIELDS},
                }
            )
    print(f"저장: train_set_{args.tag}.csv, train_answers_{args.tag}.csv", flush=True)
    if args.no_clips:
        return

    CACHE.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)
    todo = [
        (r["video_name"], r["mp4_path"])
        for r in rows
        if not (CACHE / f"{r['video_name']}.npy").exists()
    ]
    print(
        f"16장 뽑기: 남은 {len(todo)}건 (이미 있음 {len(rows) - len(todo)}건), 작업자 "
        f"{args.workers}개",
        flush=True,
    )
    t0, count, bad = time.time(), Counter(), []
    with Pool(args.workers) as pool:
        for i, (name, res) in enumerate(pool.imap_unordered(make_clip, todo, chunksize=4), 1):
            count[res.split(" ")[0]] += 1
            if res not in ("ok", "skip"):
                bad.append((name, res))
                print("  실패:", name, res, flush=True)
            if i % 200 == 0 or i == len(todo):
                el = time.time() - t0
                print(
                    f"  {i}/{len(todo)} | {el / 60:.0f}분 지남, 남은 시간 약 "
                    f"{el / i * (len(todo) - i) / 60:.0f}분",
                    flush=True,
                )
    print("끝:", dict(count))
    if bad:
        (RESULTS / "clip16_failed_tr.txt").write_text(
            "\n".join(f"{n}\t{r}" for n, r in bad), encoding="utf-8"
        )
        print("실패 목록: evals/results/vision/clip16_failed_tr.txt")


if __name__ == "__main__":
    main()
