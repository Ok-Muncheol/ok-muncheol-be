"""영상 화질 점수. 신호 칸 정확도가 화질에 비례해(흐림 1/3 0.44, 선명 1/3 0.76) 품질 게이트로 쓴다.

- sharpness: 3개 프레임(앞·중간·뒤) 라플라시안 분산의 중앙값. 클수록 선명
- kbps: 파일 크기 기준 비트레이트(10초 영상 가정이 아니라 프레임 수/fps로 길이 계산)
- collision_sec: 충돌 시각(초) 근사치. YOLO 궤적에서 상대 차 박스가 가장 커지는 순간을 쓰고(캐시:
  evals/results/trajectory_cache), 궤적이 없으면 움직임 곡선의 충돌 시작점을 쓴다(collision_source).
  이 값이 작으면 클립이 교차로 진입 뒤에 시작한 것이라 신호·차선 같은 접근 맥락이 없다(게이트 ≥ 4초)

    python -m evals.vision.quality --gt data/private/aihub/vision_gt.jsonl
→ 각 행에 "video_quality": {"sharpness", "kbps"} 를 넣어 같은 파일에 다시 쓴다.
"""

import argparse
import json
import sys
from pathlib import Path

DEFAULT_MIN_SHARPNESS = 70.0  # 실험 100건의 중앙값 근처. 게이트 기준은 결과를 보고 조정한다
DEFAULT_MIN_PRECOLLISION_SEC = 4.0  # 이 데이터셋은 충돌이 보통 4.5~5.5초에 있다
TRAJECTORY_CACHE = Path("evals/results/trajectory_cache")


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


def collision_onset(curve: list[float], fps: float) -> float:
    """움직임 곡선에서 충돌 시작 시각(초)을 고른다.

    5프레임 이동평균을 내고, 피크가 중앙값의 3배 이상으로 뚜렷하면 피크의 50%를 처음 넘는
    시점(충돌 시작), 뚜렷하지 않으면(약한 접촉) 피크 시점을 쓴다.
    첫 1초는 노출 안정화 잡음이 커서 뺀다."""
    if len(curve) < 3:
        return 0.0
    k = 5
    smooth = [sum(curve[max(0, i - k + 1):i + 1]) / len(curve[max(0, i - k + 1):i + 1])
              for i in range(len(curve))]
    start = min(len(smooth) - 1, int(round(fps)))
    tail = smooth[start:]
    peak_idx = start + max(range(len(tail)), key=tail.__getitem__)
    peak = smooth[peak_idx]
    median = sorted(tail)[len(tail) // 2]
    if peak >= 3 * median and peak > 0:
        onset = next(i for i in range(start, len(smooth)) if smooth[i] >= 0.5 * peak)
        return round(onset / fps, 2)
    return round(peak_idx / fps, 2)


def collision_sec(video_path: Path, example_id: str | None = None,
                  cache_dir: Path = TRAJECTORY_CACHE) -> tuple[float, str]:
    """(충돌 시각, 출처). example_id가 있으면 YOLO 궤적(없으면 계산해 캐시)의 최대 박스 시점을
    우선 쓴다."""
    if example_id is not None:
        from evals.vision.trajectory import cached_tracks

        tracks = cached_tracks(video_path, cache_dir, example_id)
        points = [(p["t"], p["w"] * p["h"]) for tr in tracks for p in tr["points"]]
        if points:
            return max(points, key=lambda x: x[1])[0], "yolo"
    return motion_collision_sec(video_path), "motion"


def motion_collision_sec(video_path: Path) -> float:
    import cv2

    from evals.vision.keyframes import motion_curve

    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    frames = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(cv2.resize(frame, (320, 180)))
    cap.release()
    return collision_onset(motion_curve(frames), fps) if frames else 0.0


def passes_gate(example: dict, min_sharpness: float | None = None,
                min_precollision_sec: float | None = None) -> bool:
    quality = example.get("video_quality") or {}
    if min_sharpness is not None and quality.get("sharpness", 0.0) < min_sharpness:
        return False
    if (min_precollision_sec is not None
            and quality.get("collision_sec", 0.0) < min_precollision_sec):
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gt", type=Path, default=Path("data/private/aihub/vision_gt.jsonl"))
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.gt.read_text().splitlines() if line.strip()]
    done = 0
    for row in rows:
        if not row.get("video_path"):
            continue
        quality = row.setdefault("video_quality", {})
        if "sharpness" not in quality:
            quality.update(video_quality(Path(row["video_path"])))
        if "collision_sec" not in quality or quality.get("collision_source") != "yolo":
            sec, source = collision_sec(Path(row["video_path"]), row["example_id"])
            quality["collision_sec"], quality["collision_source"] = sec, source
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
