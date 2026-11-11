"""D: 재정렬 비교 실행기. 사용법은 같은 폴더 README.md.

python -m evals.rerank_compare.run                      # 기준선 6개(벡터·BM25·RRF × 비교용·legacy)
python -m evals.rerank_compare.run --methods bm25,rrf   # 일부만
python -m evals.rerank_compare.run --candidates         # ③④ 후보 풀: BM25·RRF의 R@10~R@100
python -m evals.rerank_compare.run --retriever 모듈:팩토리 --name 이름 --max-model-calls N
python -m evals.rerank_compare.run --compare a.json b.json ...   # 저장한 결과를 표 하나로
결과는 evals/results/rerank_compare/(무시 경로)에 저장한다. 커밋하지 않는다.
"""

import argparse
import asyncio
import importlib
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from evals.rerank_compare.baselines import (  # noqa: E402
    MODES,
    QUERY_VECTORS,
    CachedEmbedder,
    PolicySearch,
    baseline_methods,
    load_vector_cache,
    save_vector_cache,
)
from evals.rerank_compare.harness import (  # noqa: E402
    RESULTS,
    SUBSETS,
    format_table,
    load_examples,
    load_result,
    recall_at,
    run_method,
    save_result,
)

CANDIDATE_KS = (10, 20, 30, 50, 100)


def make_search(allow_embedding_api: int) -> PolicySearch:
    inner = None
    if allow_embedding_api:
        from app.retrieval.embedding import OpenAIEmbedder

        inner = OpenAIEmbedder.from_env()
    embedder = CachedEmbedder(
        load_vector_cache(),
        inner=inner,
        allow_api=bool(allow_embedding_api),
        max_api_calls=allow_embedding_api,
    )
    return PolicySearch(embedder)


def finish_embedding(search: PolicySearch) -> None:
    embedder = search.embedder
    if embedder.added:
        save_vector_cache(embedder.cache)
        print(f"질의 임베딩 API {embedder.api_calls}회, 캐시에 {len(embedder.added)}개 추가")
    else:
        print("질의 임베딩 API 0회(캐시만 사용)")


def print_tables(results: list[dict]) -> None:
    for subset in SUBSETS:
        print(f"\n### {subset}")
        print(format_table(results, subset))


async def run_methods(names: list[str], args) -> None:
    search = make_search(args.allow_embedding_api)
    examples = load_examples()
    available = baseline_methods(search)
    unknown = [n for n in names if n not in available]
    if unknown:
        raise SystemExit(f"알 수 없는 방식: {unknown} (가능: {sorted(available)})")
    results = []
    for name in names:
        description = (
            "상위 10개 청크를 parent로 합침(예전 방식, 재현 확인용)"
            if name.endswith("-legacy")
            else "고유 parent 10개(비교표용)"
        )
        result = await run_method(
            name,
            available[name],  # type: ignore[arg-type]
            examples,
            max_model_calls=0,
            params=search.describe(),
            description=description,
        )
        results.append(result)
        print(f"{name}: 저장 {save_result(result)}", flush=True)
    finish_embedding(search)
    print_tables(results)


async def run_plugin(args) -> None:
    if args.max_model_calls is None:
        raise SystemExit("--retriever에는 --max-model-calls(모델 호출 상한)가 필요합니다")
    module_name, _, attr = args.retriever.partition(":")
    factory = getattr(importlib.import_module(module_name), attr)
    search = make_search(args.allow_embedding_api)
    retrieve = factory(search)
    params = dict(getattr(retrieve, "params", {}) or {}) | {"search": search.describe()}
    result = await run_method(
        args.name or attr,
        retrieve,
        load_examples(),
        max_model_calls=args.max_model_calls,
        params=params,
        description=args.description or "",
    )
    print(f"저장 {save_result(result)}")
    finish_embedding(search)
    print_tables([result])


async def run_candidates(args) -> None:
    """③④ 후보 풀 크기를 정하려고 BM25·RRF의 고유 parent R@k를 잰다.

    k마다 후보 풀을 따로 만든다(candidate_parents(n=k)와 같은 풀). RRF는 요청 청크 수에 따라 합치는
    후보 수가 달라져서, 큰 풀의 앞부분이 작은 풀과 같지 않기 때문이다.
    """
    search = make_search(args.allow_embedding_api)
    examples = load_examples()
    report: dict[str, dict] = {}
    for mode in ("bm25", "rrf"):
        pools = {
            k: {e.example_id: await search.unique_parents(e.question, mode, k) for e in examples}
            for k in CANDIDATE_KS
        }
        report[mode] = {}
        for subset in SUBSETS:
            chosen = [e for e in examples if subset == "all88" or e.subset == subset]
            row: dict[str, object] = {}
            for k in CANDIDATE_KS:
                values = [recall_at(pools[k][e.example_id], e.expected, k) for e in chosen]
                row[f"r{k}"] = sum(values) / len(chosen)
            at50 = pools[50]
            counts = [len(at50[e.example_id]) for e in chosen]
            row["parents_at_50_mean"] = statistics.fmean(counts)
            row["parents_at_50_min"] = min(counts)
            row["missed_at_50"] = [
                e.example_id for e in chosen if recall_at(at50[e.example_id], e.expected, 50) < 1
            ]
            report[mode][subset] = row
    finish_embedding(search)
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / "candidates-recall.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    ks = " | ".join(f"R@{k}" for k in CANDIDATE_KS)
    print(f"| 세트 | 방식 | {ks} | @50 parent 평균/최소 | @50에서 놓친 문항 |")
    print("| " + " | ".join(["---"] * (len(CANDIDATE_KS) + 4)) + " |")
    for subset in SUBSETS:
        for mode in ("bm25", "rrf"):
            row = report[mode][subset]
            values = " | ".join(f"{row[f'r{k}']:.3f}" for k in CANDIDATE_KS)
            print(
                f"| {subset} | {mode} | {values} | {row['parents_at_50_mean']:.1f}/"
                f"{row['parents_at_50_min']} | {len(row['missed_at_50'])} |"
            )
    print(f"저장 {path}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description="약관 재정렬 비교 평가 틀")
    parser.add_argument("--methods", help="쉼표로. 기본은 기준선 6개")
    parser.add_argument("--candidates", action="store_true", help="BM25·RRF 후보 풀 R@k")
    parser.add_argument("--retriever", help="모듈:팩토리. 팩토리(PolicySearch) → 끼우는 함수")
    parser.add_argument("--name", help="--retriever 결과 이름(파일 이름에도 쓴다)")
    parser.add_argument("--description", help="결과 JSON에 남길 설명")
    parser.add_argument("--max-model-calls", type=int, help="모델 호출 상한(넘으면 멈춤)")
    parser.add_argument(
        "--allow-embedding-api",
        type=int,
        default=0,
        metavar="N",
        help=f"캐시({QUERY_VECTORS.name})에 없는 질의를 최대 N회 임베딩 API로 받는다. 기본 0",
    )
    parser.add_argument("--compare", nargs="+", type=Path, help="저장한 결과 JSON들")
    args = parser.parse_args()
    if args.compare:
        print_tables([load_result(p) for p in args.compare])
    elif args.candidates:
        asyncio.run(run_candidates(args))
    elif args.retriever:
        asyncio.run(run_plugin(args))
    else:
        default = [*MODES, *(f"{m}-legacy" for m in MODES)]
        names = args.methods.split(",") if args.methods else default
        asyncio.run(run_methods(names, args))


if __name__ == "__main__":
    main()
