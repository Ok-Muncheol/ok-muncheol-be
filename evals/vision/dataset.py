"""AI Hub 라벨 + 코드표 + 영상 경로 → 비전 평가용 정답 카드(JSONL).

    python -m evals.vision.dataset --labels data/private/aihub/raw/labels \
        --codebook data/private/aihub/codebook.json --videos data/private/aihub/raw/videos \
        --out data/private/aihub/vision_gt.jsonl
정답·영상은 data/private/ 아래(무시 경로)에만 둔다.
"""

import argparse
import json
from pathlib import Path
from typing import Literal, TypedDict

from app.schemas.chart_scope import IN_SCOPE_ACCIDENT_TYPES, IN_SCOPE_LOCATION_TYPES
from app.schemas.facts import PartyMapping
from evals.vision.aihub import (
    AihubLabel,
    CodebookRow,
    card_fields,
    load_codebook,
    load_label,
    party_mapping,
)


class VisionExample(TypedDict):
    example_id: str
    video_path: str | None
    filming_way: Literal["bb", "cc"]
    point_of_view: Literal[1, 3]
    party_order_known: bool
    fields: dict[str, str | None]
    party_mapping: PartyMapping
    in_mvp_scope: bool
    aihub: dict[str, object]
    split: Literal["validation"]
    label_status: Literal["aihub"]


def build_example(label: AihubLabel, row: CodebookRow | None,
                  video_path: Path | None) -> VisionExample:
    fields = card_fields(row) if row else {}
    if label["accident_object"] != 0:
        fields["accident_type"] = None
    in_scope = (
        row is not None
        and fields.get("accident_type") in IN_SCOPE_ACCIDENT_TYPES
        and fields.get("location_type") in IN_SCOPE_LOCATION_TYPES
    )
    return {
        "example_id": label["video_name"],
        "video_path": None if video_path is None else str(video_path),
        "filming_way": label["filming_way"],  # type: ignore[typeddict-item]
        "point_of_view": label["point_of_view"],  # type: ignore[typeddict-item]
        # 설명서는 "1인칭이면 본인을 B로 간주"라 하지만 실제 라벨은 코드표(도표)의 A/B를 따르고
        # 촬영 차량이 A인 건이 많다(2026-11-06 육안 확인). A/B 순서는 모든 건에서 모른다고 두고
        # 채점 때 쌍을 맞바꿔도 맞은 것으로 센다. 촬영 차량 식별은 별도 라벨이 필요하다.
        "party_order_known": False,
        "fields": fields,
        "party_mapping": party_mapping(label),
        "in_mvp_scope": in_scope,
        "aihub": {
            "accident_type": label["accident_type"],
            "accident_object": label["accident_object"],
            "place_code": label["accident_place"],
            "feature_code": label["accident_place_feature"],
            "progress_a_code": label["vehicle_a_progress_info"],
            "progress_b_code": label["vehicle_b_progress_info"],
            "rate_a": label["rate_a"],
            "rate_b": label["rate_b"],
            "decoded": row,
        },
        "split": "validation",
        "label_status": "aihub",
    }


def index_videos(videos_dir: Path | None) -> dict[str, Path]:
    if videos_dir is None or not videos_dir.exists():
        return {}
    return {p.stem: p for p in videos_dir.rglob("*.mp4")}


def build(labels_dir: Path, codebook_path: Path, videos_dir: Path | None) -> list[VisionExample]:
    codebook = load_codebook(codebook_path)
    videos = index_videos(videos_dir)
    examples = []
    for path in sorted(labels_dir.rglob("*.json")):
        label = load_label(path)
        row = codebook.get(label["accident_type"]) if label["accident_type"] is not None else None
        examples.append(build_example(label, row, videos.get(label["video_name"])))
    return examples


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--codebook", type=Path, required=True)
    parser.add_argument("--videos", type=Path, default=None)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    examples = build(args.labels, args.codebook, args.videos)
    with args.out.open("w") as f:
        for ex in examples:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")
    with_video = sum(1 for e in examples if e["video_path"])
    in_scope = sum(1 for e in examples if e["in_mvp_scope"])
    print(f"{len(examples)} examples ({in_scope} in MVP scope, {with_video} with video)"
          f" -> {args.out}")


if __name__ == "__main__":
    main()
