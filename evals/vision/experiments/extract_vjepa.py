"""V-JEPA 2 특징 뽑기(evals/vision/experiments/vjepa_feat.py). 무료(노트북 GPU, 약 20~40분). 중간에
멈춰도 이어서 한다.

사용:
    .venv/Scripts/python evals/vision/experiments/extract_vjepa.py

대상: v2·v3와 같은 학습 영상(train_answers.csv + train_answers_tr.csv) + 사람 정답 92건. 16장은
clip16 저장분을 쓴다.
결과: evals/results/vision/vjepa_feat/<이름>.npz (grid 8×4×4×1024 float16, ssv2 1024) — 영상 1건 약
0.27MB
"""

import csv
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # evals/vision 공통 도구
sys.path.insert(0, str(Path(__file__).resolve().parent))  # 이 폴더의 실험 모듈
from eval_video_human import load_human  # noqa: E402
from vjepa_feat import FEAT_DIR, features  # noqa: E402

from workers.vision.video_model import CACHE  # noqa: E402

RESULTS = ROOT / "evals/results/vision"
BATCH = 8


def main():
    import numpy as np

    names = set(load_human())
    for f in ("train_answers.csv", "train_answers_tr.csv"):
        with (RESULTS / f).open(encoding="utf-8-sig") as fh:
            names |= {r["video_name"] for r in csv.DictReader(fh)}
    names = sorted(n for n in names if (CACHE / f"{n}.npy").exists())
    FEAT_DIR.mkdir(parents=True, exist_ok=True)
    todo = [n for n in names if not (FEAT_DIR / f"{n}.npz").exists()]
    print(f"대상 {len(names)}건, 남은 {len(todo)}건", flush=True)
    t0 = time.time()
    for i in range(0, len(todo), BATCH):
        chunk = todo[i : i + BATCH]
        grid, ssv2 = features(np.stack([np.load(CACHE / f"{n}.npy") for n in chunk]))
        for j, n in enumerate(chunk):
            part = FEAT_DIR / f"{n}.part.npz"  # 다 쓴 뒤 이름을 바꿔서, 멈춰도 깨진 파일이 안 남게
            np.savez(part, grid=grid[j], ssv2=ssv2[j])
            os.replace(part, FEAT_DIR / f"{n}.npz")
        done = i + len(chunk)
        if done % 400 < BATCH or done == len(todo):
            el = time.time() - t0
            print(
                f"  {done}/{len(todo)} | {el / 60:.0f}분 지남, 남은 시간 약 {el / done * (len(todo) - done) / 60:.0f}분",
                flush=True,
            )
    print("끝")


if __name__ == "__main__":
    main()
