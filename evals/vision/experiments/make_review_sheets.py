"""정답 카드 초안을 만들 때 볼 영상별 확인용 사진(프레임 30장 한 장)을 만든다.

사용:
    .venv/Scripts/python evals/vision/experiments/make_review_sheets.py
결과: data/private/aihub/review_sheets/<video>.jpg
- 영상 전체를 훑되 충돌 근처(60~90번)는 촘촘하게 넣는다.
- 라벨 박스가 있는 프레임에는 A(빨강)·B(파랑)를 그린다.
"""

import argparse
import csv
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from evals.vision.make_answer_sheet import image_index  # noqa: E402

FRAMES = [
    1,
    15,
    30,
    40,
    46,
    50,
    54,
    58,
    61,
    64,
    66,
    68,
    70,
    71,
    72,
    73,
    74,
    75,
    76,
    78,
    80,
    83,
    86,
    90,
    95,
    100,
    110,
    125,
    140,
    150,
]
COLS, W, H = 6, 384, 216


def main():
    import cv2

    parser = argparse.ArgumentParser()
    parser.add_argument("--eval-set", default=Path("evals/results/vision/eval_set.csv"), type=Path)
    parser.add_argument("--videos", default=Path("data/private/aihub/videos"), type=Path)
    parser.add_argument("--out", default=Path("data/private/aihub/review_sheets"), type=Path)
    parser.add_argument(
        "--label-zips",
        default=Path(
            "data/095.교통사고 영상 데이터/01.데이터/2.Validation/라벨링데이터_231108_add"
        ),
        type=Path,
    )
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    index = image_index(args.label_zips)

    with args.eval_set.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        name = r["video_name"]
        zpath, names = index[name]
        boxes = {}
        with zipfile.ZipFile(zpath) as zf:
            for n in names:
                d = json.loads(zf.read(n).decode("utf-8-sig"))
                boxes[d["sequence_frame_number"]] = d["objects"]
        cap = cv2.VideoCapture(str(args.videos / f"{name}.mp4"))
        tiles = []
        for f in FRAMES:
            cap.set(cv2.CAP_PROP_POS_FRAMES, f - 1)
            ok, img = cap.read()
            if not ok:
                img = 0 * cv2.imread(str(args.videos / f"{name}.mp4")) if False else None
                tiles.append(tiles[-1] * 0)
                continue
            for o in boxes.get(f, []):
                for flag, tag, color in (
                    ("isObjectA", "A", (0, 0, 255)),
                    ("isObjectB", "B", (255, 0, 0)),
                ):
                    if o.get(flag):
                        x, y, w, h = map(int, o["bbox"])
                        cv2.rectangle(img, (x, y), (x + w, y + h), color, 8)
                        cv2.putText(img, tag, (x, max(60, y - 10)), 0, 3, color, 8)
            img = cv2.resize(img, (W, H))
            cv2.rectangle(img, (0, 0), (60, 28), (0, 0, 0), -1)
            cv2.putText(img, str(f), (4, 22), 0, 0.8, (0, 255, 255), 2)
            tiles.append(img)
        sheet = cv2.vconcat([cv2.hconcat(tiles[i : i + COLS]) for i in range(0, len(tiles), COLS)])
        cv2.imwrite(str(args.out / f"{name}.jpg"), sheet, [cv2.IMWRITE_JPEG_QUALITY, 88])
    print(f"{len(rows)}장 → {args.out}")


if __name__ == "__main__":
    main()
