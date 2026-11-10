"""D: 검색 평가 실행 결과를 단계 기록 표의 줄로 요약한다.

표와 명령은 docs/retrieval-eval-report.md에 있다. 입력은
run_retrieval_evaluation.py --search-callable 결과나 예측 JSON이다.
K는 부모 수이고 청크 순위에서 부모가 처음 나온 순서로 센다.
Top-1·R@3·MRR@10은 evaluate.score_predictions와 같은 값이다.
"""

import argparse
import datetime
import hashlib
import json
import statistics
import subprocess
from pathlib import Path

from evals.retrieval.evaluate import score_predictions

ROOT = Path(__file__).resolve().parents[2]
CORPORA = {  # corpus -> (데이터 폴더, 표 이름)
    "fault_standards": ("01_fault_standards", "도표"),
    "dispute_cases": ("02_dispute_cases", "결정례"),
    "policy_terms": ("03_policy_terms", "약관"),
}
HARD = ("condition_disambiguation", "exception")  # direct가 대부분이라 평균에 묻힌다
MISS_LABELS = {"judged": "판정 오답", "outside": "후보 밖", "empty": "빈 결과"}
STEP_HEADER = ("단계", "날짜 · 커밋", "바꾼 것", "p50 / p95 ms", "질의당 비용", "채택")
CORPUS_HEADER = (
    "단계",
    "Top-1",
    "MRR@10",
    "R@3 · R@5 · R@10",
    "반환 부모 평균 / 최소",
    "직전 대비 Top-1 · @5",
    "Top-1 오답 판정 / 후보 밖 / 빈 결과",
    "조건 구분 · 예외 Top-1",
)


def load_examples(root: Path = ROOT) -> tuple[list[dict], dict[str, str]]:
    """세 코퍼스 평가셋과 파일 해시. 해시 키는 run_retrieval_evaluation.py와 같다."""
    examples, hashes = [], {}
    for folder, _ in CORPORA.values():
        data = (root / "data" / "private" / folder / "evaluation.jsonl").read_bytes()
        examples += [json.loads(s) for s in data.decode("utf-8").splitlines() if s.strip()]
        hashes[folder] = hashlib.sha256(data).hexdigest()
    return examples, hashes


def load_predictions(path: Path, hashes: dict[str, str]) -> tuple[dict, dict[str, list[str]]]:
    """(실행 결과, 예측). 실행 결과면 평가셋 해시가 지금과 같아야 한다."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if "predictions" in data:
        if data.get("dataset_hashes", hashes) != hashes:
            raise ValueError(f"{path}: 실행 때와 평가셋이 다르다. 같은 셋으로 다시 돌린다.")
        return data, data["predictions"]
    if "metrics" in data:
        raise ValueError(f"{path}: 예측이 없다. --search-callable 결과나 예측 JSON을 쓴다.")
    if not all(isinstance(v, list) for v in data.values()):
        raise ValueError(f"{path}: {{example_id: [parent_id, ...]}} 형식이 아니다.")
    return {}, data


def _rows(examples: list[dict], predictions: dict[str, list[str]]) -> dict[str, dict]:
    """정답 있는 문항별 부모 순위·첫 정답 순위·Top-1 오답 종류."""
    rows = {}
    for example in examples:
        expected = set(example["expected_parent_ids"])
        if not expected:  # 정답 없음 문항은 no_answer_accuracy로 본다(현재 0건)
            continue
        parents = list(dict.fromkeys(predictions.get(example["example_id"], [])))
        rank = next((i + 1 for i, p in enumerate(parents) if p in expected), None)
        assessed = example.get("review", {}).get("candidate_assessments", [])
        pool = {a["parent_id"] for a in assessed}
        miss = None
        if rank != 1:
            miss = "empty" if not parents else "judged" if parents[0] in pool else "outside"
        rows[example["example_id"]] = {
            "corpus": example["request"]["corpus"],
            "category": example.get("question_category"),
            "expected": expected,
            "parents": parents,
            "rank": rank,
            "miss": miss,
        }
    return rows


def _recall(row: dict, k: int) -> float:
    return len(row["expected"].intersection(row["parents"][:k])) / len(row["expected"])


def _change(pairs: list[tuple[dict, dict]], hit) -> tuple[int, int]:
    """(새로 맞힌 문항 수, 새로 놓친 문항 수). pairs는 (지금, 직전)."""
    return (
        sum(hit(now) and not hit(before) for now, before in pairs),
        sum(hit(before) and not hit(now) for now, before in pairs),
    )


def summarize(
    examples: list[dict],
    predictions: dict[str, list[str]],
    previous: dict[str, list[str]] | None = None,
    *,
    allow_machine_reviewed: bool = False,
) -> dict[str, dict]:
    """코퍼스별 지표·반환 부모 수·Top-1 오답 종류·어려운 범주·직전 대비 변화."""
    for run in (predictions, previous):
        if run is not None:  # 검수·분할·예측 ID 검사는 채점기와 같이 한다
            score_predictions(examples, run, allow_machine_reviewed=allow_machine_reviewed)
    now = _rows(examples, predictions)
    before = None if previous is None else _rows(examples, previous)
    summary = {}
    for corpus in CORPORA:
        ids = [i for i, row in now.items() if row["corpus"] == corpus]
        if not ids:
            continue
        rows = [now[i] for i in ids]
        n = len(rows)
        counts = [len(row["parents"]) for row in rows]
        delta = None
        if before is not None:
            pairs = [(now[i], before[i]) for i in ids]
            delta = {
                "top1": _change(pairs, lambda row: row["rank"] == 1),
                "at5": _change(pairs, lambda row: _recall(row, 5) > 0),
            }
        summary[corpus] = {
            "n": n,
            "top1": sum(row["rank"] == 1 for row in rows) / n,
            "mrr_at_10": sum(1 / r for r in (row["rank"] for row in rows) if r and r <= 10) / n,
            "recall": {k: sum(_recall(row, k) for row in rows) / n for k in (3, 5, 10)},
            "parents_mean": statistics.mean(counts),
            "parents_min": min(counts),
            "top1_misses": {m: sum(row["miss"] == m for row in rows) for m in MISS_LABELS},
            "hard_top1": {
                c: (
                    sum(row["rank"] == 1 for row in rows if row["category"] == c),
                    sum(row["category"] == c for row in rows),
                )
                for c in HARD
            },
            "delta": delta,
        }
    return summary


def _row(cells: tuple[str, ...]) -> str:
    return "| " + " | ".join(cells) + " |"


def render(
    summary: dict[str, dict], result: dict, *, step: str, change: str, commit: str, today: str
) -> list[str]:
    """4절 단계 목록 한 줄과 5절 코퍼스별 표에 붙일 줄."""
    latency = result.get("latency_ms")
    timing = f"{latency['p50']:.0f} / {latency['p95']:.0f}" if latency else "—"
    lines = ["4. 단계 목록", _row((step, f"{today} · `{commit}`", change, timing, "", ""))]
    for corpus, (_, name) in CORPORA.items():
        s = summary.get(corpus)
        if s is None:
            continue
        delta = s["delta"]
        cells = (
            step,
            f"{s['top1']:.2f}",
            f"{s['mrr_at_10']:.2f}",
            " · ".join(f"{s['recall'][k]:.2f}" for k in (3, 5, 10)),
            f"{s['parents_mean']:.1f} / {s['parents_min']}",
            "—" if delta is None else " · ".join(f"+{g} -{lost}" for g, lost in delta.values()),
            " / ".join(str(count) for count in s["top1_misses"].values()),
            " · ".join(f"{hit}/{total}" for hit, total in s["hard_top1"].values()),
        )
        lines += [f"5. {name} (n={s['n']})", _row(cells)]
    return lines


def misses(examples: list[dict], predictions: dict[str, list[str]]) -> list[str]:
    """Top-1 오답 문항(로컬 확인용). 문서에는 붙이지 않는다."""
    return [
        "\t".join(
            (
                example_id,
                row["corpus"],
                row["category"] or "-",
                f"정답 {row['rank'] or '-'}위",
                row["parents"][0] if row["parents"] else "-",
                MISS_LABELS[row["miss"]],
            )
        )
        for example_id, row in _rows(examples, predictions).items()
        if row["miss"]
    ]


def _git(*args: str) -> str:
    done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _commit() -> str:
    """짧은 커밋 해시. 커밋하지 않은 변경이 있으면 +를 붙인다."""
    try:
        return _git("rev-parse", "--short", "HEAD") + ("+" if _git("status", "--porcelain") else "")
    except (OSError, subprocess.CalledProcessError):
        return "?"


def main() -> None:
    parser = argparse.ArgumentParser(description="검색 평가 결과를 단계 기록 표의 줄로 요약")
    parser.add_argument("result", type=Path, help="run_retrieval_evaluation.py 결과나 예측 JSON")
    parser.add_argument("--previous", type=Path, help="직전 단계 결과. 문항별 개선/악화를 센다")
    parser.add_argument("--step", default="?", help="단계 번호")
    parser.add_argument("--change", default="", help="이 단계에서 바꾼 것")
    parser.add_argument(
        "--allow-machine-reviewed",
        action="store_true",
        help="예측 JSON을 기계 검수 기준으로 채점(실행 결과는 label_tier를 따른다)",
    )
    parser.add_argument("--misses", action="store_true", help="Top-1 오답 문항(로컬 확인용)")
    args = parser.parse_args()
    examples, hashes = load_examples()
    result, predictions = load_predictions(args.result, hashes)
    previous = load_predictions(args.previous, hashes)[1] if args.previous else None
    allow = args.allow_machine_reviewed or result.get("label_tier") == "machine_reference"
    summary = summarize(examples, predictions, previous, allow_machine_reviewed=allow)
    today = datetime.date.today().isoformat()
    lines = render(
        summary, result, step=args.step, change=args.change, commit=_commit(), today=today
    )
    if args.misses:
        lines += ["", "Top-1 오답 (로컬 확인용, 문서에 붙이지 않는다)"]
        lines += misses(examples, predictions)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
