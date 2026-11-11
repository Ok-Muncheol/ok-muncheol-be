"""움직임 분류기 학습용 데이터 준비: 1인칭 영상(평가 30건 제외)을 꺼내고 YOLO·카메라 흐름을 계산해
저장한다. 무료.

사용:
    .venv/Scripts/python evals/vision/prepare_train.py # 이미 처리한 영상은 건너뜀(중간에 멈춰도
    이어서)

- 대상: labels.csv의 1인칭 중 select_eval_set.reject_reason 통과, eval_set.csv(평가 30건) 제외 →
  train_set.csv
- 정답: make_answer_sheet.prefill(라벨 사고 유형 → 칸, 사람 정답과 움직임 90% 일치) →
  train_answers.csv
- 영상: data/private/aihub/videos/, YOLO: evals/results/vision/yolo/, 카메라 흐름:
  evals/results/vision/ego_cache/
평가 30건은 학습에 절대 넣지 않는다(최종 평가 오염 방지).
"""

import csv
import shutil
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_answer_sheet import prefill  # noqa: E402
from select_eval_set import reject_reason  # noqa: E402
from silver import ego_cached  # noqa: E402

from workers.vision.detection import detect_and_track  # noqa: E402

RESULTS = ROOT / "evals/results/vision"
VIDEOS = ROOT / "data/private/aihub/videos"
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


def main():
    with (RESULTS / "labels.csv").open(encoding="utf-8-sig") as f:
        labels = list(csv.DictReader(f))
    with (RESULTS / "eval_set.csv").open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames
        held_out = {r["video_name"] for r in reader}
    rows = sorted(
        (
            r
            for r in labels
            if r["video_point_of_view"] == "1"
            and r["video_name"] not in held_out
            and reject_reason(r) is None
        ),
        key=lambda r: r["video_name"],
    )
    with (RESULTS / "train_set.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows({**r, "split": "train"} for r in rows)
    with (RESULTS / "train_answers.csv").open("w", newline="", encoding="utf-8-sig") as f:
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
    print(f"대상 {len(rows)}건 → train_set.csv, train_answers.csv", flush=True)

    t0 = time.time()
    for i, r in enumerate(rows, 1):
        name = r["video_name"]
        dest = VIDEOS / f"{name}.mp4"
        try:
            if not dest.exists():
                zpath, inner = r["mp4_path"].split("::", 1)
                with zipfile.ZipFile(zpath) as zf, zf.open(inner) as fin, dest.open("wb") as fout:
                    shutil.copyfileobj(fin, fout)
            detect_and_track(str(dest), cache_path=RESULTS / "yolo" / f"{name}.json")
            ego_cached(name)
        except Exception as e:  # 한 건 실패로 멈추지 않는다
            print(f"{name} 실패: {e!r}", flush=True)
        if i % 50 == 0:
            el = time.time() - t0
            print(
                f"{i}/{len(rows)} {el / 60:.0f}분 (남은 시간 약 {el / i * (len(rows) - i) / 60:.0f}분)",
                flush=True,
            )
    print(f"끝 {(time.time() - t0) / 60:.0f}분", flush=True)


if __name__ == "__main__":
    main()
