"""움직임 B 확인 표시에 '사고 유형 자신감'을 더하면 나아지는지 본다. 무료(저장된 예측만 읽음, 몇 초).

사용(먼저 train_type_head.py가 type_head_preds.json을 만들어야 함):
    .venv/Scripts/python evals/vision/eval_b_typeflag.py

B 값은 그대로(분류기), 표시만 비교한다. 정답 = 사람 92건(다시 본 라벨 반영).
- 지금: 분류기 ≠ 영상 모델 v2면 표시(v2가 '기타'면 카메라 규칙과 비교, fact_card.py와 같음)
- 규칙 1: 지금 + 유형 B 자신감 < 0.9면 표시
- 규칙 2: 지금 + 유형이 고른 B 값 ≠ 카드 B 값이면 표시
미리 정한 판정(2026-11-10, 결과 보기 전): 표시 없이 틀린 B가 3건 이상 줄고, 늘어난 표시가 15건 이하면 채택.
둘 다 되면 '늘어난 표시 1개당 새로 잡은 오류'가 많은 쪽. 0.9 경계는 구간표를 본 뒤 정한 것이라 조금 유리할 수 있음
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from eval_video_human import load_human  # noqa: E402
from silver import ego_cached  # noqa: E402

RESULTS = ROOT / "evals/results/vision"
THRESH = 0.9


def main():
    human = load_human()
    old = json.loads((RESULTS / "video_human_preds.json").read_text(encoding="utf-8"))
    typed = json.loads((RESULTS / "type_head_preds.json").read_text(encoding="utf-8"))
    names = sorted(n for n in human if n in old and n in typed and human[n].get("movement_B"))

    def rule(n):
        return ego_cached(n).field_opinions(human[n]["location_type"])["movement_B"]["value"]

    rows = []
    for n in names:
        card = old[n].get("classifier_B") or rule(n)
        vb = old[n]["video"]["movement_B"]
        vb = rule(n) if vb == "기타" else vb
        t = typed[n]["movement_B"]
        rows.append({"wrong": card != human[n]["movement_B"], "now": vb != card,
                     "low": (t["confidence"] or 0) < THRESH, "diff": bool(t["value"]) and t["value"] != card})
    N = len(rows)
    wrong = sum(r["wrong"] for r in rows)
    print(f"사람 정답 {N}건, 카드 B 틀림 {wrong}건 (맞음 {N - wrong})")

    def report(title, flag):
        f = [flag(r) for r in rows]
        flags = sum(f)
        missed = sum(r["wrong"] and not x for r, x in zip(rows, f))
        clean = [r for r, x in zip(rows, f) if not x]
        acc = sum(not r["wrong"] for r in clean) / max(len(clean), 1)
        print(f"  {title:28} 표시 {flags:3}건 | 표시 없이 틀림 {missed:3}건 | 표시 없는 칸 정확도 {acc:.0%}")
        return flags, missed

    f0, m0 = report("지금(분류기≠v2)", lambda r: r["now"])
    verdict, best = [], None
    for title, flag in (("규칙 1: + 유형 자신감 < 0.9", lambda r: r["now"] or r["low"]),
                        ("규칙 2: + 유형 B ≠ 카드 B", lambda r: r["now"] or r["diff"])):
        f1, m1 = report(title, flag)
        added, caught = f1 - f0, m0 - m1
        ok = caught >= 3 and added <= 15
        rate = caught / max(added, 1)
        verdict.append(f"{title}: 새로 잡은 오류 {caught}건 / 늘어난 표시 {added}건 → {'통과' if ok else '탈락'}")
        if ok and (best is None or rate > best[1]):
            best = (title, rate)
    print("\n판정(미리 정한 기준):")
    for v in verdict:
        print("  " + v)
    print(f"  → {best[0].split(':')[0] + ' 채택' if best else '지금 규칙 유지'}")


if __name__ == "__main__":
    main()
