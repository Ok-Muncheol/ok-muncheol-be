"""영상 화질 점수. 신호 칸 정확도가 화질에 비례해(흐림 1/3 0.44, 선명 1/3 0.76) 품질 게이트로 쓴다.

- sharpness: 3개 프레임(앞·중간·뒤) 라플라시안 분산의 중앙값. 클수록 선명
- kbps: 파일 크기 기준 비트레이트(10초 영상 가정이 아니라 프레임 수/fps로 길이 계산)

    python -m evals.vision.quality --gt data/private/aihub/vision_gt.jsonl
→ 각 행에 "video_quality": {"sharpness", "kbps"} 를 넣어 같은 파일에 다시 쓴다.
"""

import argparse
import json
import sys
from pathlib import Path

DEFAULT_MIN_SHARPNESS = 68.0  # 실험 100건의 중앙값. 게이트 기준은 결과를 보고 조정한다


def video_quality(video_path: Path) -> dict[str, float]:
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(str(video_path))
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    values = []
    for idx in (frames // 15, frames // 2, frames - frames // 15 - 1):
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, idx))
        ok, frame = cap.read()
        if ok:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            values.append(cv2.Laplacian(gray, cv2.CV_64F).var())
    cap.release()
    seconds = frames / fps if frames else 10.0
    return {
        "sharpness": round(float(np.median(values)), 1) if values else 0.0,
        "kbps": round(video_path.stat().st_size * 8 / seconds / 1000),
    }


def passes_gate(example: dict, min_sharpness: float) -> bool:
    quality = example.get("video_quality") or {}
    return quality.get("sharpness", 0.0) >= min_sharpness


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gt", type=Path, default=Path("data/private/aihub/vision_gt.jsonl"))
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.gt.read_text().splitlines() if line.strip()]
    done = 0
    for row in rows:
        if row.get("video_path") and not row.get("video_quality"):
            row["video_quality"] = video_quality(Path(row["video_path"]))
            done += 1
            if done % 50 == 0:
                print(f"{done}...", file=sys.stderr)
    args.gt.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    scored = [r["video_quality"]["sharpness"] for r in rows if r.get("video_quality")]
    scored.sort()
    print(f"{done}건 계산, 총 {len(scored)}건. sharpness 분위(10/25/50/75/90%): "
          f"{[scored[int(len(scored) * q)] for q in (0.1, 0.25, 0.5, 0.75, 0.9)]}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
