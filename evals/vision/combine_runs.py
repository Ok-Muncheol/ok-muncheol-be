"""여러 실행 폴더의 카드를 한 폴더에 모아 함께 채점한다. 무료. 원래 폴더는 안 바꾼다.

사용(개발 세트 30건 = 개선용 10 + 최종용 20, 2026-11-10 결정):
    .venv/Scripts/python evals/vision/combine_runs.py <폴더1> <폴더2> --out dev30_<이름>

결과: evals/results/vision/runs/<out>/ (카드 사본 + score.md, score.csv)
같은 영상이 두 폴더에 있으면 뒤에 쓴 폴더의 카드를 쓴다.
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "evals/results/vision/runs"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dirs", nargs="+", type=Path)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out = RUNS / args.out
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.json"):  # 이전에 모은 카드는 지우고 다시 모은다(이 폴더 안만)
        old.unlink()
    n = 0
    for d in args.run_dirs:
        for p in sorted(d.glob("*.json")):
            if p.name != "run.json":
                shutil.copy2(p, out / p.name)
                n += 1
    print(f"카드 {n}장 → {out}")
    subprocess.run([sys.executable, str(ROOT / "evals/vision/score.py"), str(out)], check=True)


if __name__ == "__main__":
    main()
