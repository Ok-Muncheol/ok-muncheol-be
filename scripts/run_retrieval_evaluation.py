"""RAG 함수 또는 저장된 부모 ID 순위로 평가. 운영 검색 구현은 담당자 함수 사용."""

import argparse
import asyncio
import hashlib
import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from embed_corpora import LOCATIONS, read  # noqa: E402 -- standalone CLI adds repository root before app imports

from evals.retrieval.evaluate import evaluate, score_predictions  # noqa: E402 -- standalone CLI adds repository root before app imports


async def main():
    parser = argparse.ArgumentParser(description="RAG 담당자 검색 결과로 고정 평가셋 채점")
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--predictions", type=Path, help="JSON 객체 {example_id: [parent_id, ...]}")
    inputs.add_argument(
        "--search-callable",
        help="예: app.retrieval.common:search. SearchRequest -> list[EvidenceHit]",
    )
    parser.add_argument(
        "--allow-machine-reviewed",
        action="store_true",
        help="사람 정답과 구분된 기계 검수 기준으로 채점",
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "data/private/retrieval_evaluation_results.json"
    )
    args = parser.parse_args()
    examples = []
    fingerprints = {}
    for folder, _ in LOCATIONS:
        path = ROOT / "data/private" / folder / "evaluation.jsonl"
        examples += read(path)
        fingerprints[folder] = hashlib.sha256(path.read_bytes()).hexdigest()
    if args.predictions:
        predictions = json.loads(args.predictions.read_text(encoding="utf-8"))
        if not isinstance(predictions, dict) or any(
            not isinstance(v, list) or not all(isinstance(p, str) for p in v)
            for v in predictions.values()
        ):
            parser.error("predictions는 {example_id: [parent_id,...]} 형식이어야 합니다.")
        result = {
            "metrics": score_predictions(
                examples, predictions, allow_machine_reviewed=args.allow_machine_reviewed
            ),
            "label_tier": "machine_reference"
            if any(e["label_status"] == "machine_reviewed" for e in examples)
            else "reviewed",
        }
    else:
        module, name = args.search_callable.split(":", 1)
        search = getattr(importlib.import_module(module), name)
        result = await evaluate(
            examples, search_fn=search, allow_machine_reviewed=args.allow_machine_reviewed
        )
    result["dataset_hashes"] = fingerprints
    result["example_count"] = len(examples)
    result["limitations"] = [
        "기계 검수 기준은 전문가 정답과 동일하지 않음",
        "후보 원문 대조 범위 밖의 정답 누락 가능",
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "example_count": len(examples),
                "label_tier": result["label_tier"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
