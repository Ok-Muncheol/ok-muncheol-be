"""이미 만든 카드에 장소 정리 규칙을 적용해 다시 채점한다. 질문을 새로 돌리지 않는다(무료).

사용:
    .venv/Scripts/python evals/vision/experiments/rescore_confirmed.py
    evals/results/vision/runs/<run_id>

두 가지 결과 폴더를 만든다(원래 폴더는 건드리지 않음):
- <run_id>__tidy       자동 결과: 카드가 답한 장소로 정리 규칙만 적용(fields.tidy_by_location)
- <run_id>__confirmed  장소 확인 후: 담당자가 장소를 정답 값으로 고쳤다고 보고 정리 규칙 적용.
                       장소가 바뀐 카드는 장소에 딸린 칸(PLACE_FIELDS)에 확인 표시를 붙인다(담당자가
                       같이 봄).
각 폴더를 score.py로 채점한다.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from evals.vision.score import load_answers  # noqa: E402
from workers.vision.fact_card import needs_review  # noqa: E402
from workers.vision.fields import PLACE_FIELDS, tidy_by_location  # noqa: E402


def apply(card: dict, place: str | None) -> dict:
    """place가 있으면 담당자가 장소를 그 값으로 고친 것으로 본다."""
    fields = card["fields"]
    moved = False
    if place and fields["location_type"]["value"] != place:
        fields["location_type"]["value"] = place
        moved = True
    if place:
        fields["location_type"]["verification_status"] = "confirmed"
    #  움직임 A·B: 담당자가 확인한 장소의 예측으로 바꾼다(카드에 장소별 예측이 있을 때, 기타면
    # 그대로).
    #  지금 값의 출처(A: VLM, B: 카메라 규칙)가 직진·좌·우 밖의 특별한 움직임이면 덮지
    # 않는다(fact_card와 같음)
    for field in ("movement_A", "movement_B"):
        d = fields[field].get("vision_detail") or {}
        by_place = d.get("by_place")
        base = (
            (d.get("vlm") or {}).get("value")
            if field == "movement_A"
            else (d.get("detection") or {}).get("rule")
        )
        special = base not in (None, "불명", "직진", "좌회전", "우회전")
        if (
            place
            and by_place
            and not special
            and (by_place.get(place) or {}).get("value") not in (None, "기타")
        ):
            fields[field]["value"] = by_place[place]["value"]
    values = {k: f["value"] for k, f in fields.items()}
    for name, (old, new) in tidy_by_location(values).items():
        f = fields[name]
        f["value"] = new
        f.setdefault("vision_detail", {})["tidy"] = {"from": old, "to": new}
        values[name] = new
    # 확인 표시를 지금 규칙(fact_card.needs_review)과 바뀐 값으로 다시 매긴다
    for name, f in fields.items():
        if name == "narrative" or (
            name == "location_type" and f["verification_status"] == "confirmed"
        ):
            continue
        d = f.get("vision_detail") or {}
        flag = needs_review(
            name,
            f["value"],
            f.get("model_confidence"),
            d.get("agree"),
            values,
            viewpoint=1 if "_1_" in card["case_id"] else 3,
            has_det=d.get("detection") is not None,
        )
        f["verification_status"] = "needs_review" if flag else "unverified"
    if moved:
        for name in PLACE_FIELDS:
            fields[name]["verification_status"] = "needs_review"
    return card


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument(
        "--answers", default=ROOT / "evals/vision/answers/answer_cards.xlsx", type=Path
    )
    args = parser.parse_args()
    answers = load_answers(args.answers)

    for mode in ("tidy", "confirmed"):
        out = args.run_dir.parent / f"{args.run_dir.name}__{mode}"
        out.mkdir(exist_ok=True)
        changed = 0
        for path in sorted(args.run_dir.glob("*.json")):
            if path.name == "run.json":
                continue
            card = json.loads(path.read_text(encoding="utf-8"))
            ans = answers.get(card["case_id"])
            if not ans:
                continue
            place = ans.get("location_type") if mode == "confirmed" else None
            before = json.dumps(card["fields"], ensure_ascii=False)
            card = apply(card, place)
            changed += before != json.dumps(card["fields"], ensure_ascii=False)
            (out / path.name).write_text(
                json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        print(f"\n######## {mode}: 카드 {changed}건 바뀜 → {out}")
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "evals/vision/score.py"),
                str(out),
                "--answers",
                str(args.answers),
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
