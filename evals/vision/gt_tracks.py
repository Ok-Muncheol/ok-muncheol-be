"""AI Hub 이미지 라벨(정답 박스)에서 A·B 궤적을 꺼낸다. 실험 2단계 입력.

입력표 기준(블랙박스 차=B)으로 맞춘다: 1인칭에서 라벨 박스가 B에만 있으면 A/B를 바꾼다
(make_answer_sheet.label_swapped와 같은 규칙).
"""

import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from evals.vision.make_answer_sheet import image_index, label_swapped  # noqa: E402

LABEL_ZIPS = Path("data/095.교통사고 영상 데이터/01.데이터/2.Validation/라벨링데이터_231108_add")
_INDEX = None


def load_gt_tracks(row: dict, root: Path) -> tuple[dict, tuple[int, int]]:
    """(궤적, (가로, 세로)). row는 eval_set.csv 한 줄."""
    global _INDEX
    if _INDEX is None:
        _INDEX = image_index(root / LABEL_ZIPS)
    zpath, names = _INDEX[row["video_name"]]
    swap = label_swapped(row)
    tracks = {"A": [], "B": []}
    size = (1920, 1080)
    with zipfile.ZipFile(zpath) as zf:
        for n in names:
            d = json.loads(zf.read(n).decode("utf-8-sig"))
            size = (d.get("width") or size[0], d.get("height") or size[1])
            for o in d.get("objects", []):
                for flag, party in (("isObjectA", "A"), ("isObjectB", "B")):
                    if o.get(flag):
                        if swap:
                            party = "B" if party == "A" else "A"
                        tracks[party].append((d["sequence_frame_number"], *map(float, o["bbox"])))
    return {k: sorted(v) for k, v in tracks.items() if v}, size
