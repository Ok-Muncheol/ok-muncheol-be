"""D 시연·확인: 실제 색인 + OpenAI 질의 임베더로 공통 검색을 돌려 본다. CI에서 실행하지 않는다.

실행: py -3.12 scripts/try_retrieval.py          (시연 질문 5개, C의 검색 도구를 거침)
      py -3.12 scripts/try_retrieval.py --eval   (도표·결정례·약관 평가셋 채점)
      py -3.12 scripts/try_retrieval.py --eval --mode bm25 [--tokenizer simple]  (임베딩 없음)
      py -3.12 scripts/try_retrieval.py --compare  (벡터·BM25·RRF 비교, 같은 질의 벡터 사용)
      py -3.12 scripts/try_retrieval.py --short    (짧은 질문 30개로 세 방식 비교, 참고용)
시연에는 "면책 근거 확인"도 포함한다: 무면허·음주·고의 판정기에 넘어가는 근거 목록(판정은 안 함).
준비: py -3.12 -m pip install -e ".[embedding]", .env에 OPENAI_API_KEY
- 임베딩 API만 호출한다(LLM 호출 없음). 키 값은 출력하지 않는다.
- 평가 라벨은 기계 검수 기준이라 전문가 정답과 같지 않다. 짧은 질문은 Claude가 쓰고 정답을
  붙였다(사람 검수 아님).
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

from app.calculation.exclusions import fetch_exclusion_clauses  # noqa: E402
from app.calculation.rules import DEFAULT_RULES  # noqa: E402
from app.retrieval.common import configure_retrieval, fetch_by_parent, search  # noqa: E402
from app.retrieval.embedding import (  # noqa: E402
    EmbeddingMismatch,
    EmbeddingRequestFailed,
    OpenAIEmbedder,
    RetrievalNotConfigured,
)
from app.retrieval.hybrid import RetrievalOptions, keyword_corpora_for  # noqa: E402
from app.retrieval.keyword import SimpleTokenizer  # noqa: E402
from app.retrieval.loader import handoff_terms_edition, load_index, read_handoff  # noqa: E402
from app.tools.dispute_cases import search_dispute_cases  # noqa: E402
from app.tools.fault_standards import search_fault_standards  # noqa: E402
from app.tools.policy_terms import search_policy_terms  # noqa: E402
from evals.retrieval.evaluate import evaluate  # noqa: E402

ACCIDENT_DATE = "2026-10-20"
QUESTIONS = [
    ("policy_terms", "음주운전 중 사고 자기차량손해 보상하지 않는 손해"),
    ("policy_terms", "무면허 운전 면책"),
    ("policy_terms", "자기부담금"),
    ("fault_standards", "신호등 있는 교차로 녹색 직진 대 적색 직진"),
    ("dispute_cases", "교차로 신호위반 직진 차량 충돌"),
]
EVAL_CORPORA = ("fault_standards", "dispute_cases", "policy_terms")
TOP = 3
TOOLS = {
    "policy_terms": search_policy_terms,
    "fault_standards": search_fault_standards,
    "dispute_cases": search_dispute_cases,
}
SHORT_QUERIES = ROOT / "scripts/d_short_queries.jsonl"
SHORT_TOP = 10
# 비교용 이전 점수(Top-1, Recall@3, Hit@3, MRR@10), 데이터 1f7b2dd. 도표는 첫 측정.
PREVIOUS = {
    "dispute_cases": (0.550, 0.730, 0.730, 0.670),
    "policy_terms": (0.500, 0.619, 0.625, 0.585),
}


MODES = {
    "vector": RetrievalOptions(mode="vector"),
    "bm25": RetrievalOptions(mode="bm25"),
    "rrf": RetrievalOptions(mode="rrf"),
}


def connect(corpora=None, *, mode: str = "vector", tokenizer=None) -> None:
    options = MODES[mode]
    started = time.perf_counter()
    keyword = keyword_corpora_for(options)  # 벡터만 쓰면 BM25 통계를 만들지 않는다
    index = load_index(corpora, tokenizer=tokenizer, keyword_corpora=keyword)
    used = index.keyword_version if keyword else "없음(벡터만)"
    print(f"적재 {time.perf_counter() - started:.1f}초 · 키워드 {used}")
    configure_retrieval(
        index=index,
        embedder=OpenAIEmbedder.from_env() if options.needs_vector else None,
        options=options,
        default_terms_edition=handoff_terms_edition(),
    )


def preview(text: str) -> str:
    return " ".join(text.split())[:80]


async def run_questions(mode: str, tokenizer) -> None:
    connect(mode=mode, tokenizer=tokenizer)
    print(f"방식 {mode}")
    for corpus, text in QUESTIONS:
        print(f"\n[{corpus}] {text}")
        await show(corpus, text)
    await show_exclusion_evidence()


def clause_label(chunk: dict) -> str:
    metadata = chunk["metadata"]
    kind = metadata.get("clause_type")
    if kind == "rider":
        return f"특약 {metadata.get('rider_number')}"
    if kind == "side_note":
        return "비고·각주(보조)"
    if kind == "cited_law_appendix":
        return "인용 법조문(보조)"
    return f"제{metadata.get('article_number')}조"


async def show_exclusion_evidence() -> None:
    """review_exclusions와 같은 함수로 근거를 모은다. 판정기(LLM)는 부르지 않는다."""
    print("\n[면책 근거 확인] 판정기에 넘어가는 순서 그대로(조항 ID 직접 조회, validate_hits 통과)")
    for topic in DEFAULT_RULES.exclusion_topics:
        hits = await fetch_exclusion_clauses(topic, edition=None, fetch=fetch_by_parent)
        print(f"\n  {topic}: 근거 {len(hits)}개")
        if not hits:
            print("    (조항을 찾지 못함 — 면책 확인 필요)")
        for rank, hit in enumerate(hits, 1):
            chunk = hit["chunk"]
            print(f"    {rank}. {clause_label(chunk)} · p.{chunk['page_start']}")
            print(f"       {preview(chunk['text'])}")


async def show(corpus: str, text: str) -> None:
    hits = await TOOLS[corpus](
        {"text": text, "accident_date": ACCIDENT_DATE, "filters": {}, "top_k": TOP}
    )
    if not hits:
        print("  (결과 없음)")
    for rank, hit in enumerate(hits, 1):
        chunk = hit["chunk"]
        pages = f"{chunk['page_start']}-{chunk['page_end']}"
        role = chunk["metadata"].get("retrieval_role", "-")
        print(f"  {rank}. {chunk['parent_id']} · p.{pages} · {hit['score']:.4f} · {role}")
        print(f"     {preview(chunk['text'])}")


def read_rows(corpus: str, role_prefix: str) -> list[dict]:
    entry = next(c for c in read_handoff()["corpora"] if c["corpus"] == corpus)
    path = next(f["path"] for f in entry["files"] if f["role"].startswith(role_prefix))
    lines = (ROOT / path).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def row(values: tuple[float, ...]) -> str:
    return " | ".join(f"{value:.3f}" for value in values)


async def run_evaluation(mode: str, tokenizer) -> None:
    connect(EVAL_CORPORA, mode=mode, tokenizer=tokenizer)
    print(f"방식 {mode}, 기계 검수 라벨 기준(전문가 정답 아님). 약관은 기본 판.")
    print("| 코퍼스 | 문항 | Top-1 | Recall@3 | Hit@3 | MRR@10 | 이전 | p50 ms |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for corpus in EVAL_CORPORA:
        examples = read_rows(corpus, "evaluation questions")
        result = await evaluate(examples, search_fn=search, allow_machine_reviewed=True)
        metrics = result["metrics"][corpus]
        scores = tuple(
            metrics[name] for name in ("top1_accuracy", "recall_at_3", "hit_at_3", "mrr_at_10")
        )
        previous = PREVIOUS.get(corpus)
        print(
            f"| {corpus} | {len(examples)} | {row(scores)} | "
            f"{row(previous) if previous else '없음(첫 측정)'} | "
            f"{result['latency_ms']['p50']:.0f} |",
            flush=True,
        )


def overlap(tokenizer, question: str, text: str) -> float:
    """질문 내용어 중 본문에 그대로 있는 비율."""
    terms = set(tokenizer.tokenize(question))
    return len(terms & set(tokenizer.tokenize(text))) / len(terms) if terms else 0.0


def parent_texts(corpus: str) -> dict[str, str]:
    by_parent: dict[str, str] = {}
    for chunk in read_rows(corpus, "searchable chunks"):
        by_parent[chunk["parent_id"]] = by_parent.get(chunk["parent_id"], "") + chunk["text"]
    return by_parent


def overlap_rates(
    tokenizer, by_parent: dict[str, str], questions: list[tuple[str, list[str]]]
) -> tuple[float, float]:
    """(정답 조항 비율, 다른 조항 표본 비율). 표본은 질문마다 정답이 아닌 조항 최대 5개."""
    parents = sorted(by_parent)
    gold, other = [], []
    for number, (question, expected) in enumerate(questions):
        text = " ".join(by_parent[p] for p in expected)
        gold.append(overlap(tokenizer, question, text))
        wrong = [p for p in parents[number::37] if p not in expected][:5]
        other += [overlap(tokenizer, question, by_parent[p]) for p in wrong]
    return sum(gold) / len(gold), sum(other) / max(len(other), 1)


def first_correct(example: dict, predictions: dict) -> bool:
    actual = predictions.get(example["example_id"], [])
    return bool(actual) and actual[0] in example["expected_parent_ids"]


async def run_compare(tokenizer) -> None:
    """세 방식을 같은 색인·같은 질의 벡터로 비교한다(임베더 캐시). 파일에 저장하지 않는다."""
    index = load_index(EVAL_CORPORA, tokenizer=tokenizer)
    configure_retrieval(
        index=index,
        embedder=OpenAIEmbedder.from_env(),
        default_terms_edition=handoff_terms_edition(),
    )
    names = ("top1_accuracy", "recall_at_3", "mrr_at_10")
    print(f"기계 검수 라벨 기준(전문가 정답 아님). 키워드 {index.keyword_version}. 약관은 기본 판.")
    print("| 코퍼스 | 방식 | Top-1 | Recall@3 | MRR@10 | Top-1 나아짐/나빠짐(벡터 대비) |")
    print("| --- | --- | --- | --- | --- | --- |")
    categories: dict[str, dict[str, dict[str, list[int]]]] = {}
    for corpus in EVAL_CORPORA:
        examples = read_rows(corpus, "evaluation questions")
        vector_predictions = {}
        for mode, options in MODES.items():
            configure_retrieval(options=options)
            result = await evaluate(examples, search_fn=search, allow_machine_reviewed=True)
            predictions = result["predictions"]
            metrics = result["metrics"][corpus]
            if mode == "vector":
                vector_predictions = predictions
                change = "-"
            else:
                better = sum(
                    first_correct(e, predictions) and not first_correct(e, vector_predictions)
                    for e in examples
                )
                worse = sum(
                    first_correct(e, vector_predictions) and not first_correct(e, predictions)
                    for e in examples
                )
                change = f"+{better} / -{worse}"
            print(
                f"| {corpus} | {mode} | {row(tuple(metrics[n] for n in names))} | {change} |",
                flush=True,
            )
            for e in examples:
                bucket = categories.setdefault(corpus, {}).setdefault(
                    str(e.get("question_category")), {}
                )
                bucket.setdefault(mode, []).append(first_correct(e, predictions))
    print("\n질문 유형별 Top-1 (벡터 / BM25 / RRF, 문항 수)")
    for corpus, buckets in categories.items():
        for category, by_mode in sorted(buckets.items()):
            scores = " / ".join(f"{sum(by_mode[m]) / len(by_mode[m]):.2f}" for m in MODES)
            print(f"  {corpus} · {category}: {scores} ({len(by_mode['vector'])}문항)")
    print("\n질문 내용어가 본문에 그대로 있는 비율(평가셋 영향 가늠)")
    for corpus in EVAL_CORPORA:
        examples = read_rows(corpus, "evaluation questions")
        questions = [(e["request"]["text"], e["expected_parent_ids"]) for e in examples]
        gold, other = overlap_rates(index.tokenizer, parent_texts(corpus), questions)
        print(f"  {corpus}: 정답 조항 {gold:.2f} · 다른 조항 표본 {other:.2f}")


def short_scores(ranked: list[str], expected: list[str]) -> tuple[bool, bool, float]:
    """(Top-1, Hit@3, 역순위@10). 같은 조항의 여러 청크는 첫 순위만 쓴다."""
    ranked = list(dict.fromkeys(ranked))
    rank = next((i for i, p in enumerate(ranked[:SHORT_TOP], 1) if p in expected), None)
    return rank == 1, rank is not None and rank <= 3, 1 / rank if rank else 0.0


async def run_short(tokenizer) -> None:
    """짧은 질문으로 세 방식을 비교한다. 같은 색인·질의 벡터(임베더 캐시), 파일 저장 없음."""
    lines = SHORT_QUERIES.read_text(encoding="utf-8").splitlines()
    queries = [json.loads(line) for line in lines if line.strip()]
    index = load_index(EVAL_CORPORA, tokenizer=tokenizer)
    configure_retrieval(
        index=index,
        embedder=OpenAIEmbedder.from_env(),
        default_terms_edition=handoff_terms_edition(),
    )
    print("참고용: 질문과 정답 모두 Claude가 작성, 사람 검수 아님. 1문항 = 약 0.1.")
    print(f"키워드 {index.keyword_version}. 약관은 기본 판. 정답 없는 질문은 채점에서 뺀다.")
    first: dict[str, dict[str, str]] = {}
    print("\n| 코퍼스 | 방식 | 채점 문항 | Top-1 | Hit@3 | MRR@10 |")
    print("| --- | --- | --- | --- | --- | --- |")
    for corpus in EVAL_CORPORA:
        rows = [q for q in queries if q["corpus"] == corpus]
        for mode, options in MODES.items():
            configure_retrieval(options=options)
            scores = []
            for query in rows:
                request = {
                    "corpus": corpus,
                    "text": query["text"],
                    "accident_date": "",
                    "filters": {},
                    "top_k": SHORT_TOP,
                }
                ranked = [hit["chunk"]["parent_id"] for hit in await search(request)]
                first.setdefault(query["id"], {})[mode] = ranked[0] if ranked else "-"
                if query["expected_parent_ids"]:
                    scores.append(short_scores(ranked, query["expected_parent_ids"]))
            top1, hit3, mrr = (sum(s[i] for s in scores) / len(scores) for i in range(3))
            print(f"| {corpus} | {mode} | {len(scores)} | {row((top1, hit3, mrr))} |", flush=True)
    print("\n문항별 1위 (O = 정답, 정답 없는 질문은 1위만)")
    for query in queries:
        expected = query["expected_parent_ids"]
        cells = []
        for mode in MODES:
            parent = first[query["id"]][mode]
            mark = "" if not expected else (" O" if parent in expected else " X")
            cells.append(f"{mode} {parent.split(':', 1)[-1]}{mark}")
        print(f"  [{query['id']}] {query['text']}\n      " + " | ".join(cells))
    print("\n질문 내용어가 정답 원문에 그대로 있는 비율(지난 평가셋과 비교)")
    print("| 코퍼스 | 짧은 질문: 정답 / 다른 조항 | 지난 평가셋: 정답 / 다른 조항 |")
    print("| --- | --- | --- |")
    for corpus in EVAL_CORPORA:
        by_parent = parent_texts(corpus)
        short = [
            (q["text"], q["expected_parent_ids"])
            for q in queries
            if q["corpus"] == corpus and q["expected_parent_ids"]
        ]
        examples = read_rows(corpus, "evaluation questions")
        old = [(e["request"]["text"], e["expected_parent_ids"]) for e in examples]
        new_gold, new_other = overlap_rates(index.tokenizer, by_parent, short)
        old_gold, old_other = overlap_rates(index.tokenizer, by_parent, old)
        print(f"| {corpus} | {new_gold:.2f} / {new_other:.2f} | {old_gold:.2f} / {old_other:.2f} |")


def main() -> None:
    parser = argparse.ArgumentParser(description="실제 색인으로 공통 검색 확인(임베딩 API만 호출)")
    parser.add_argument("--eval", action="store_true", help="도표·결정례·약관 평가셋 채점")
    parser.add_argument("--compare", action="store_true", help="벡터·BM25·RRF 비교")
    parser.add_argument("--short", action="store_true", help="짧은 질문 30개 비교(참고용)")
    parser.add_argument("--mode", choices=sorted(MODES), default="rrf")
    parser.add_argument("--tokenizer", choices=("auto", "simple"), default="auto")
    args = parser.parse_args()
    try:
        tokenizer = SimpleTokenizer() if args.tokenizer == "simple" else None
        if args.short:
            asyncio.run(run_short(tokenizer))
        elif args.compare:
            asyncio.run(run_compare(tokenizer))
        elif args.eval:
            asyncio.run(run_evaluation(args.mode, tokenizer))
        else:
            asyncio.run(run_questions(args.mode, tokenizer))
    except (RetrievalNotConfigured, EmbeddingMismatch, EmbeddingRequestFailed) as exc:
        raise SystemExit(f"실행 불가: {exc}") from None


if __name__ == "__main__":
    main()
