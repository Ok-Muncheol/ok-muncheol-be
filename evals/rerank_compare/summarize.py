"""D: 결과 원본 JSON(무시 경로) → 원문 없는 실행 요약(runs/<날짜>/, 커밋 대상).

    python -m evals.rerank_compare.summarize evals/rerank_compare/runs/2026-11-12/manifest.json

manifest의 실행마다 evals/results/rerank_compare/<file>을 읽어 manifest 옆 <out>에 쓴다.
남기는 것: 지표·설정(params)·사용량·비용·git_commit·평가셋·공통 조건·코퍼스 파일 sha256과 판,
질문별 example_id·묶음·첫 정답 순위. 질문 문장·정답 목록·순위 목록·후보 글·단계별 parent 목록은
넣지 않는다(원본 JSON의 원문과 라벨은 로컬에만 둔다).
"""

import argparse
import hashlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.rerank_compare.harness import RESULTS, SCHEMA  # noqa: E402

SUMMARY_SCHEMA = "rerank-compare-summary-v1"
# corpus_records.jsonl sha256 → 코퍼스 판(김지은 님 zip 이름의 시각). 세 판은 도표 레코드만 다르고
# 청크·문서·쪽 파일은 같다(약관 본문 같음)
CORPUS_EDITIONS = {
    "b6d4c4cbdbf47e133b5456ed2318f14049e64466ff90a05a6236166dd0c8a8a4": "1413",
    "26c249db6bf1d5240ff9abaf2c1ee449bb31d4cf1bc806c4ba17288421ceebce": "1655",
    "a8e90a011e452d4737258ca939ed2aa9248a65b8e9f5e2d15a83a599786edaac": "1057",
}
# 요약에 있으면 안 되는 칸(원문·라벨·순위 목록)
FORBIDDEN_KEYS = frozenset(
    {"question", "text", "expected", "ranked", "kept_parents", "accident_date", "questions_raw"}
)


def corpus_edition(corpus: dict) -> str | None:
    return CORPUS_EDITIONS.get(corpus.get("corpus_records.jsonl", ""))


def _walk_keys(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _walk_keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _walk_keys(inner)


def check_no_raw(summary: dict, question_texts: Iterator[str] | list[str] = ()) -> None:
    """원문·라벨 칸이 남았거나 평가 질문 문장이 들어 있으면 멈춘다.

    칸 이름 검사는 params·common 밖만 본다(점수기 설명의 "question" 같은 요청 형식 설명이 있다).
    질문 문장 검사는 요약 전체 글에서 한다.
    """
    outside = {k: v for k, v in summary.items() if k not in ("params", "common")}
    found = FORBIDDEN_KEYS.intersection(_walk_keys(outside))
    if found:
        raise ValueError(f"요약에 원문·라벨 칸이 있습니다: {sorted(found)}")
    blob = json.dumps(summary, ensure_ascii=False)
    leaked = sum(1 for text in question_texts if text and text in blob)
    if leaked:
        raise ValueError(f"요약에 평가 질문 문장 {leaked}개가 있습니다")


def summarize(result: dict, *, source: str, sha256: str, entry: dict) -> dict:
    """결과 JSON 하나 → 요약. entry는 manifest 항목(번호·역할 등, 그대로 남긴다).

    결과에 있는 질문 문장이 요약에 들어가지 않았는지도 확인한다.
    """
    if result.get("schema") != SCHEMA:
        raise ValueError(f"{source}: {SCHEMA} 결과가 아닙니다")
    corpus = result.get("corpus") or {}
    summary = {
        "schema": SUMMARY_SCHEMA,
        "run": {k: v for k, v in entry.items() if k not in ("file", "out")},
        "source": {"file": source, "sha256": sha256},
        "method": result["method"],
        "description": result.get("description", ""),
        "created_at": result.get("created_at"),
        "git_commit": result.get("git_commit"),
        "dataset": result.get("dataset"),
        "corpus_files_sha256": corpus,
        "corpus_edition": corpus_edition(corpus),
        "common": result.get("common"),
        "params": result.get("params"),
        "metrics": result["metrics"],
        "questions": [
            {
                "example_id": q["example_id"],
                "subset": q["subset"],
                "first_correct_rank": q["scores"]["first_correct_rank"],
            }
            for q in result["questions"]
        ],
    }
    check_no_raw(summary, [q.get("question", "") for q in result["questions"]])
    return summary


def write_summaries(manifest_path: Path, results: Path = RESULTS) -> list[Path]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    written = []
    for entry in manifest["runs"]:
        raw = (results / entry["file"]).read_bytes()
        summary = summarize(
            json.loads(raw),
            source=entry["file"],
            sha256=hashlib.sha256(raw).hexdigest(),
            entry=entry,
        )
        out = manifest_path.parent / entry["out"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        written.append(out)
    return written


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description="결과 원본 JSON → 원문 없는 실행 요약")
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    for path in write_summaries(args.manifest):
        print(path)


if __name__ == "__main__":
    main()
