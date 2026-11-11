"""움직임 A: VLM vs 영상 모델, 사람 정답 92건 비교. 무료(저장된 카드만 읽음).

사용(먼저 run_cards.py로 62건 카드를 만든다, 유료 약 2,300원):
    .venv/Scripts/python evals/vision/run_cards.py --split human --eval-set
    evals/results/vision/human62_set.csv --ego --yolo --motion --video
    .venv/Scripts/python evals/vision/experiments/eval_motion_a_92.py evals/results/vision/runs/<위
    실행 폴더>

미리 정한 방식(결과를 보고 바꾸지 않는다, 2026-11-10):
- 사람 정답: 우리 30건 + 구준휘님 64건(겹치면 구준휘님) = 92건. VLM 답: 30건은 11/9 실행(RUNS),
  62건은 위 실행
- 영상 모델 답: eval_video_human.py가 저장한 video_human_preds.json('기타'면 직진)
- 정확 맥니마 검정. 판정: 영상 모델이 더 맞히고 p < 0.10 → 움직임 A를 영상 모델로 바꿈
  그 밖 → VLM 유지, 둘이 다르면 확인 표시(움직임 A는 지금 항상 표시라, 같을 때 정확도가 높으면
  표시를 줄일 근거가 됨)
- 이 92건 중 이미 쓴 30건이 섞여 있으므로 62건만의 결과도 함께 출력한다
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # evals/vision 공통 도구
sys.path.insert(0, str(Path(__file__).resolve().parent))  # 이 폴더의 실험 모듈
from eval_video_human import load_human, mcnemar  # noqa: E402
from exp_human_mix import RUNS  # noqa: E402

RESULTS = ROOT / "evals/results/vision"


def read_cards(run_dir: Path) -> dict:
    """{영상: (VLM 답, 카드 최종 답, 확인 표시 여부)}"""
    out = {}
    for p in run_dir.glob("*.json"):
        if p.name == "run.json":
            continue
        c = json.loads(p.read_text(encoding="utf-8"))
        f = c["fields"]["movement_A"]
        out[c["case_id"]] = (
            (f["vision_detail"].get("vlm") or {}).get("value"),
            f["value"],
            f["verification_status"] == "needs_review",
        )
    return out


def compare(title, names, truth, vlm, video):
    v_ok = {n: vlm[n] == truth[n] for n in names}
    m_ok = {n: video[n] == truth[n] for n in names}
    b = sum(m_ok[n] and not v_ok[n] for n in names)  # 영상 모델만 맞힘
    c = sum(v_ok[n] and not m_ok[n] for n in names)  # VLM만 맞힘
    p = mcnemar(b, c)
    N = len(names)
    print(f"\n== {title} ({N}건)")
    print(
        f"  VLM {sum(v_ok.values())}/{N} ({sum(v_ok.values()) / N:.0%}) | 영상 모델 {sum(m_ok.values())}/{N} "
        f"({sum(m_ok.values()) / N:.0%}) | 전부 직진 {sum(truth[n] == '직진' for n in names)}/{N}"
    )
    print(
        f"  영상 모델만 맞힘 {b} | VLM만 맞힘 {c} | 둘 다 맞힘 {sum(v_ok[n] and m_ok[n] for n in names)} | "
        f"둘 다 틀림 {sum(not v_ok[n] and not m_ok[n] for n in names)} | p = {p:.3f}"
    )
    for cl in ("직진", "좌회전", "우회전"):
        sel = [n for n in names if truth[n] == cl]
        print(
            f"    {cl} {len(sel)}건 맞힘: VLM {sum(vlm[n] == cl for n in sel)}, 영상 모델 {sum(video[n] == cl for n in sel)}"
        )
    agree = [n for n in names if vlm[n] == video[n]]
    print(
        f"  (확인 표시 참고) 둘이 같은 {len(agree)}건 정답률 {sum(v_ok[n] for n in agree)}/{len(agree)} | "
        f"다른 {N - len(agree)}건"
    )
    return b, c, p


def main():
    if len(sys.argv) < 2:
        raise SystemExit("사용: eval_motion_a_92.py <62건 실행 폴더>")
    new_run = Path(sys.argv[1])
    human = load_human()
    cards = {}
    for run in RUNS:
        cards.update(read_cards(RESULTS / "runs" / run))
    new = read_cards(new_run)
    cards.update(new)  # 겹치면 새 실행
    preds = json.loads((RESULTS / "video_human_preds.json").read_text(encoding="utf-8"))
    video = {n: r["video"]["movement_A"] for n, r in preds.items()}
    names = sorted(n for n in human if n in cards and n in video and cards[n][0])
    missing = sorted(n for n in human if n not in names)
    print(
        f"비교 {len(names)}건 (VLM 답 없음·실패 {len(missing)}건{': ' + ', '.join(missing) if missing else ''})"
    )
    truth = {n: human[n]["movement_A"] for n in names}
    vlm = {n: cards[n][0] for n in names}

    b, c, p = compare("전체 92건", names, truth, vlm, video)
    print(
        "  판정:",
        "움직임 A를 영상 모델로 바꿈" if b > c and p < 0.10 else "VLM 유지(둘이 다르면 확인 표시)",
    )
    compare(
        "참고: 새 62건만(30건은 이미 쓴 세트)", [n for n in names if n in new], truth, vlm, video
    )

    final_ok = sum(cards[n][1] == truth[n] for n in names)
    flagged = [n for n in names if cards[n][2]]
    print(f"\n(참고) 카드 최종값: {final_ok}/{len(names)}, 확인 표시 {len(flagged)}건")


if __name__ == "__main__":
    main()
