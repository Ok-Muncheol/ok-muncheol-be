"""C: 동일 질의/필터로 기준선·개선안을 비교한다. 미라벨 양식은 채점하지 않는다."""

import inspect
import statistics
import time

from evals.retrieval.dataset import RetrievalExample


def score_predictions(
    examples: list[RetrievalExample],
    predictions: dict[str, list[str]],
    *,
    allow_machine_reviewed: bool = False,
) -> dict[str, object]:
    """RAG 담당자가 반환한 부모 ID 순위로 채점. 검색 알고리즘은 구현하지 않는다.

    복수 청크의 같은 부모는 첫 등장 순위로 합친다. 누락된 검색 결과는 실패로
    계산하며, 검수한 정답 없음 질의는 별도 정확도를 계산한다.
    """
    if not examples:
        raise ValueError("평가셋이 비어 있습니다.")
    ids = set()
    groups = {}
    for example in examples:
        machine = example["label_status"] == "machine_reviewed"
        acceptable = example["label_status"] == "reviewed" or (
            machine and allow_machine_reviewed and example.get("review", {}).get("grounded") is True
        )
        if not acceptable or example["split"] == "unassigned":
            raise ValueError("미검수·미분할 평가셋은 채점할 수 없습니다.")
        if example["example_id"] in ids:
            raise ValueError("평가 ID 중복")
        ids.add(example["example_id"])
        group = example["split_group_id"]
        if group in groups and groups[group] != example["split"]:
            raise ValueError("같은 자료 계열이 여러 분할에 포함되었습니다.")
        groups[group] = example["split"]
    if set(predictions) - ids:
        raise ValueError("평가셋에 없는 예측 ID가 있습니다.")
    buckets = {}
    for example in examples:
        corpus = example["request"]["corpus"]
        bucket = buckets.setdefault(
            corpus,
            {
                "positive_count": 0,
                "top1_hits": 0,
                "hit3": 0,
                "mrr10": 0.0,
                "recall3_sum": 0.0,
                "negative_count": 0,
                "negative_correct": 0,
            },
        )
        actual = list(dict.fromkeys(predictions.get(example["example_id"], [])))
        expected = set(example["expected_parent_ids"])
        if not expected:
            bucket["negative_count"] += 1
            bucket["negative_correct"] += int(not actual)
        else:
            bucket["positive_count"] += 1
            bucket["top1_hits"] += int(bool(actual) and actual[0] in expected)
            bucket["recall3_sum"] += len(expected.intersection(actual[:3])) / len(expected)
            bucket["hit3"] += int(bool(expected.intersection(actual[:3])))
            bucket["mrr10"] += next(
                (1 / (i + 1) for i, p in enumerate(actual[:10]) if p in expected), 0
            )
    return {
        corpus: {
            "positive_count": b["positive_count"],
            "negative_count": b["negative_count"],
            "top1_accuracy": b["top1_hits"] / b["positive_count"] if b["positive_count"] else None,
            "recall_at_3": b["recall3_sum"] / b["positive_count"] if b["positive_count"] else None,
            "hit_at_3": b["hit3"] / b["positive_count"] if b["positive_count"] else None,
            "mrr_at_10": b["mrr10"] / b["positive_count"] if b["positive_count"] else None,
            "no_answer_accuracy": b["negative_correct"] / b["negative_count"]
            if b["negative_count"]
            else None,
        }
        for corpus, b in buckets.items()
    }


async def evaluate(
    examples: list[RetrievalExample], *, search_fn=None, allow_machine_reviewed: bool = False
) -> dict[str, object]:
    """검수 라벨·정답 ID/적용판·분할 누수 검사 후 Top-1·Recall@3·시간/비용 측정.

    unlabelled·unassigned는 거절한다. 정답 없음은 검수한 음성 사례만 허용한다.
    """
    # 검수·분할 검증을 먼저 수행한다. 잘못된 셋으로 외부 호출하지 않는다.
    score_predictions(examples, {}, allow_machine_reviewed=allow_machine_reviewed)
    if search_fn is None:
        from app.retrieval.common import search

        search_fn = search
    predictions = {}
    durations = []
    for example in examples:
        started = time.perf_counter()
        hits = search_fn(example["request"])
        if inspect.isawaitable(hits):
            hits = await hits
        predictions[example["example_id"]] = [hit["chunk"]["parent_id"] for hit in hits]
        durations.append((time.perf_counter() - started) * 1000)
    ordered = sorted(durations)
    return {
        "metrics": score_predictions(
            examples, predictions, allow_machine_reviewed=allow_machine_reviewed
        ),
        "label_tier": "machine_reference"
        if any(e["label_status"] == "machine_reviewed" for e in examples)
        else "reviewed",
        "latency_ms": {
            "p50": statistics.median(durations),
            "p95": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        },
        "predictions": predictions,
    }
