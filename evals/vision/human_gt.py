"""사람 라벨을 AI Hub 정답셋에 입혀 채점용 정답셋(vision_gt_human.jsonl)을 만든다.

사람 라벨(`evals/vision/labels/human_labels_*.jsonl`)은 영상 ID와 우리가 단 값만 담고 있어 저장소에
들어간다. AI Hub 영상·라벨은 재배포가 안 되므로 각자 내려받아 `vision_gt.jsonl`을 만든 뒤
이 스크립트로 합친다.

규칙:
- 촬영 차량을 B로 고정한다(모델 규약). 사람 라벨의 ego_party가 A면 AI Hub의 A/B 칸을 맞바꾼다.
- 진행(movement)은 사람 라벨로 덮어쓴다. 신호·진입 순서는 AI Hub 값을 촬영 차량 기준으로 옮긴다.
- 메모에 "선진입/후진입"이 있고 AI Hub 진입 순서가 비어 있으면 그 값으로 채운다.
- party_order_known=true로 두어 채점에서 A/B 맞바꿈을 정답으로 치지 않는다.
- status가 scored·duplicate인 행만 넣는다(excluded·unknown은 뺀다).
  duplicate는 채점기가 기본 규칙으로 뺀다.

사용:
    python -m evals.vision.human_gt --gt data/private/aihub/vision_gt.jsonl \
        --labels evals/vision/labels/human_labels_2026-11-09.jsonl \
        --out data/private/aihub/vision_gt_human.jsonl
"""

from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path

ENTRY_SWAP = {"first": "second", "second": "first"}
ENTRY_NOTE = re.compile(r"(선진입|선집입|후진입)")
USABLE_STATUS = ("scored", "duplicate")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def apply_label(gt_row: dict, label: dict) -> dict:
    """AI Hub 정답 행 하나에 사람 라벨 하나를 입힌 새 행을 돌려준다."""
    ego = label["ego_party"]
    if ego not in ("A", "B"):
        raise ValueError(f"ego_party must be A or B: {label}")
    other = "A" if ego == "B" else "B"
    row = copy.deepcopy(gt_row)
    f = row["fields"]
    fields = dict(f)
    fields["movement_B"] = label["movement_ego"]
    fields["movement_A"] = label["movement_other"]
    fields["signal_B"] = f.get(f"signal_{ego}")
    fields["signal_A"] = f.get(f"signal_{other}")
    entry = f.get("entry_order")
    if ego == "A" and entry in ENTRY_SWAP:
        entry = ENTRY_SWAP[entry]
    note = label.get("note") or ""
    match = ENTRY_NOTE.search(note)
    if match and entry is None:
        # entry_order는 A 기준이므로 촬영 차량(B)이 선진입이면 second
        entry = "second" if match.group(1) in ("선진입", "선집입") else "first"
    fields["entry_order"] = entry
    row["fields"] = fields
    row["party_order_known"] = True
    row["party_mapping"] = {**row.get("party_mapping", {}), "video_ego_party_id": ego}
    row["human_label"] = {
        "ego_party_chart": ego, "status": label.get("status"), "note": note,
        "labeled_at": label.get("labeled_at"),
        "aihub_movement": {"A": f.get("movement_A"), "B": f.get("movement_B")},
    }
    return row


def build(gt_rows: list[dict], labels: list[dict]) -> tuple[list[dict], list[str]]:
    """(정답 행 목록, 정답셋에 없는 라벨 ID 목록)."""
    by_id = {r["example_id"]: r for r in gt_rows}
    out, missing = [], []
    for label in labels:
        if label.get("status") not in USABLE_STATUS:
            continue
        gt_row = by_id.get(label["example_id"])
        if gt_row is None:
            missing.append(label["example_id"])
            continue
        out.append(apply_label(gt_row, label))
    return out, missing


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gt", type=Path, default=Path("data/private/aihub/vision_gt.jsonl"))
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--out", type=Path,
                        default=Path("data/private/aihub/vision_gt_human.jsonl"))
    args = parser.parse_args()
    rows, missing = build(read_jsonl(args.gt), read_jsonl(args.labels))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    print(f"{len(rows)} rows -> {args.out}")
    if missing:
        print(f"정답셋에 없는 라벨 {len(missing)}건 (AI Hub 영상을 덜 받았는지 확인): "
              f"{missing[:5]}")


if __name__ == "__main__":
    main()
