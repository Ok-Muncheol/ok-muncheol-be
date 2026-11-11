"""평가 영상 목록(eval_set.csv)에 있는 mp4만 zip에서 꺼낸다. 원본 zip은 건드리지 않는다.

사용:
    python evals/vision/extract_videos.py
    python evals/vision/extract_videos.py --eval-set evals/results/vision/eval_set.csv --out data/private/aihub/videos
"""

import argparse
import csv
import shutil
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval-set", default=Path("evals/results/vision/eval_set.csv"), type=Path)
    parser.add_argument("--out", default=Path("data/private/aihub/videos"), type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    with args.eval_set.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    done = skipped = 0
    for r in rows:
        dest = args.out / f"{r['video_name']}.mp4"
        if dest.exists():
            skipped += 1
            continue
        src = r["mp4_path"]
        if "::" in src:  # zip 안의 파일: "zip경로::안쪽이름"
            zpath, inner = src.split("::", 1)
            with zipfile.ZipFile(zpath) as zf, zf.open(inner) as fin, dest.open("wb") as fout:
                shutil.copyfileobj(fin, fout)
        else:
            shutil.copy2(src, dest)
        done += 1
        print(f"[{r['split']}] {dest.name} ({dest.stat().st_size / 1e6:.1f}MB)")

    print(f"\n꺼냄 {done}건, 이미 있음 {skipped}건 → {args.out}")


if __name__ == "__main__":
    main()
