"""D: 검색 방식(①~⑥·기준선)을 같은 질문·같은 지표·같은 JSON 모양으로 채점한다.

끼우는 함수(Retriever): retrieve(question: Question, meter: Meter) -> list[str]
- 상위 parent_id를 순위대로 최대 10개(TOP_K). 동기·async 둘 다 받는다.
  같은 parent가 반복되면 첫 순위.
- 모델을 부를 때마다 meter.add(model, input_tokens=, output_tokens=).
  상한을 넘으면 그 자리에서 멈춘다.
지표: Top-1, R@3, R@5, R@10, MRR@10(정의는 evals/retrieval/evaluate.score_predictions와 같다),
질문당 시간, 모델 호출 수, 토큰 수. 점수는 약관 80(policy80)·별표 8(schedule8)·합계 88(all88) 따로,
도표는 fault100, 결정례는 dispute100(코퍼스는 common.set_corpus, run.py --corpus).
"""

import datetime
import hashlib
import inspect
import json
import statistics
import subprocess
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from evals.rerank_compare.common import (
    CORPUS,
    DISPUTE,
    FAULT,
    TOP_K,
    active_corpus,
    common_settings,
)

ROOT = Path(__file__).resolve().parents[2]
QUESTIONS = ROOT / "data/private/03_policy_terms/evaluation.jsonl"  # 88문항
QUESTIONS_80 = ROOT / "data/private/03_policy_terms/evaluation-80.jsonl"  # 별표 8문항을 가리는 기준
DB_IMPORT = ROOT / "data/private/corpus-db-import/db_import"
RESULTS = ROOT / "evals/results/rerank_compare"  # 무시 경로(커밋 금지)
# 코퍼스별 평가셋(100문항씩). 필터(document_id)는 파일의 request.filters 그대로
CORPUS_QUESTIONS = {
    FAULT: ROOT / "data/private/01_fault_standards/evaluation.jsonl",
    DISPUTE: ROOT / "data/private/02_dispute_cases/evaluation.jsonl",
}
# 코퍼스별 문항 묶음. 마지막이 전체
CORPUS_SUBSETS = {
    CORPUS: ("policy80", "schedule8", "all88"),
    FAULT: ("fault100",),
    DISPUTE: ("dispute100",),
}
SUBSETS = tuple(s for subsets in CORPUS_SUBSETS.values() for s in subsets)
SCHEMA = "rerank-compare-v1"


@dataclass(frozen=True)
class Question:
    """끼우는 함수가 받는 것. 정답·세트 이름은 넣지 않는다(채점 쪽 Example에만 있다)."""

    example_id: str
    text: str
    accident_date: str  # ISO 날짜 또는 ""(별표3 과실상계 문항 하나만 값이 있다)
    filters: dict  # 평가셋 request.filters 그대로(document_id)


@dataclass(frozen=True)
class Example:
    question: Question
    subset: str  # "policy80" | "schedule8" | "fault100" | "dispute100"
    expected: tuple[str, ...]  # 정답 parent_id

    @property
    def example_id(self) -> str:
        return self.question.example_id


class ModelCallLimitExceeded(RuntimeError):
    """실행 전 정한 모델 호출 상한을 넘었다."""


@dataclass
class Meter:
    """모델 호출 수·토큰 수. 끼우는 함수가 호출할 때마다 add한다. max_calls=None이면 상한 없음."""

    max_calls: int | None = None
    calls: dict[str, int] = field(default_factory=dict)
    input_tokens: int | None = None  # 한 번도 보고하지 않으면 None(토큰 수 없음)
    output_tokens: int | None = None
    # 단계별로 남긴 parent(계층 탐색용). 채점 때만 정답과 비교한다(stage 참고)
    stages: list[dict] = field(default_factory=list)

    def stage(self, name: str, kept_parents: Sequence[str]) -> None:
        """이 단계 뒤에 아직 도달할 수 있는 parent를 남긴다. 정답은 끼우는 함수가 보지 않는다."""
        self.stages.append({"name": name, "kept_parents": list(dict.fromkeys(kept_parents))})

    @property
    def total_calls(self) -> int:
        return sum(self.calls.values())

    def add(
        self,
        model: str,
        *,
        calls: int = 1,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        if self.max_calls is not None and self.total_calls + calls > self.max_calls:
            raise ModelCallLimitExceeded(f"모델 호출 상한 {self.max_calls}회를 넘습니다")
        self.calls[model] = self.calls.get(model, 0) + calls
        if input_tokens is not None:
            self.input_tokens = (self.input_tokens or 0) + input_tokens
        if output_tokens is not None:
            self.output_tokens = (self.output_tokens or 0) + output_tokens

    def snapshot(self) -> tuple[dict[str, int], int | None, int | None]:
        return dict(self.calls), self.input_tokens, self.output_tokens


Retriever = Callable[[Question, Meter], Sequence[str] | Awaitable[Sequence[str]]]


def _read_jsonl(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_examples(
    path: Path | None = None, path_80: Path = QUESTIONS_80, *, corpus: str | None = None
) -> list[Example]:
    """지금 코퍼스의 평가셋. 약관 88문항은 evaluation-80.jsonl의 ID가 policy80, 나머지 schedule8."""
    corpus = corpus or active_corpus()
    if corpus != CORPUS:
        return _load_corpus_examples(path or CORPUS_QUESTIONS[corpus], corpus)
    path = path or QUESTIONS
    old = {row["example_id"] for row in _read_jsonl(path_80)}
    examples = []
    for row in _read_jsonl(path):
        request = row["request"]
        if request["corpus"] != "policy_terms":
            raise ValueError(f"{row['example_id']}: 약관 문항이 아닙니다")
        question = Question(
            example_id=row["example_id"],
            text=request["text"],
            accident_date=request.get("accident_date") or "",
            filters=dict(request.get("filters") or {}),
        )
        subset = "policy80" if row["example_id"] in old else "schedule8"
        examples.append(Example(question, subset, tuple(row["expected_parent_ids"])))
    if not old <= {e.example_id for e in examples}:
        raise ValueError("evaluation-80.jsonl의 문항이 88문항 파일에 없습니다")
    return examples


def _load_corpus_examples(path: Path, corpus: str) -> list[Example]:
    (subset,) = CORPUS_SUBSETS[corpus]
    examples = []
    for row in _read_jsonl(path):
        request = row["request"]
        if request["corpus"] != corpus:
            raise ValueError(f"{row['example_id']}: {corpus} 문항이 아닙니다")
        question = Question(
            example_id=row["example_id"],
            text=request["text"],
            accident_date=request.get("accident_date") or "",
            filters=dict(request.get("filters") or {}),
        )
        examples.append(Example(question, subset, tuple(row["expected_parent_ids"])))
    return examples


# ---- 지표 ----


def rank_scores(ranked: Sequence[str], expected: Sequence[str]) -> dict[str, float | int | None]:
    """한 문항의 점수. ranked는 중복 제거된 parent 순위."""
    gold = set(expected)
    if not gold:
        raise ValueError("정답 없는 문항은 채점하지 않는다")
    first = next((i for i, p in enumerate(ranked, 1) if p in gold), None)
    scores: dict[str, float | int | None] = {"first_correct_rank": first}
    scores["top1"] = float(first == 1)
    for k in (3, 5, 10):
        scores[f"r{k}"] = len(gold.intersection(ranked[:k])) / len(gold)
    scores["mrr10"] = 1 / first if first is not None and first <= 10 else 0.0
    return scores


def recall_at(ranked: Sequence[str], expected: Sequence[str], k: int) -> float:
    gold = set(expected)
    return len(gold.intersection(ranked[:k])) / len(gold)


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]  # evaluate.py와 같은 정의


def summarize(rows: list[dict]) -> dict[str, object]:
    """질문별 행 → 지표. 토큰은 한 문항이라도 보고하지 않았으면 None."""
    n = len(rows)
    seconds = [r["seconds"] for r in rows]
    calls_by_model: dict[str, int] = {}
    for row in rows:
        for model, count in row["model_calls_by_model"].items():
            calls_by_model[model] = calls_by_model.get(model, 0) + count
    calls = sum(calls_by_model.values())

    def tokens(key: str) -> int | None:
        values = [r[key] for r in rows]
        return None if any(v is None for v in values) else sum(values)

    metrics: dict[str, object] = {"n": n}
    for key in ("top1", "r3", "r5", "r10", "mrr10"):
        metrics[key] = sum(r["scores"][key] for r in rows) / n
    metrics |= {
        "seconds_mean": statistics.fmean(seconds),
        "seconds_p50": statistics.median(seconds),
        "seconds_p95": _p95(seconds),
        "seconds_total": sum(seconds),
        "model_calls": calls,
        "model_calls_per_question": calls / n,
        "model_calls_by_model": calls_by_model,
        "input_tokens": tokens("input_tokens"),
        "output_tokens": tokens("output_tokens"),
        "stage_survival": stage_survival(rows),
    }
    return metrics


def stage_survival(rows: list[dict]) -> dict[str, float] | None:
    """단계별 생존율: 그 단계 뒤에 정답 parent가 아직 남은 비율의 평균. 단계 기록이 없으면 None."""
    names = list(dict.fromkeys(s["name"] for r in rows for s in r["stages"]))
    if not names:
        return None
    out = {}
    for name in names:
        values = [s["survival"] for r in rows for s in r["stages"] if s["name"] == name]
        if len(values) != len(rows):
            raise ValueError(f"단계 {name}을 기록하지 않은 문항이 있습니다")
        out[name] = sum(values) / len(values)
    return out


def _score_stages(stages: list[dict], expected: Sequence[str]) -> list[dict]:
    """채점 때만 정답과 비교한다. 생존율 = 남은 parent 안에 든 정답 비율."""
    gold = set(expected)
    return [
        {
            "name": s["name"],
            "kept_count": len(s["kept_parents"]),
            "survival": len(gold.intersection(s["kept_parents"])) / len(gold),
            "kept_parents": s["kept_parents"],
        }
        for s in stages
    ]


def subset_metrics(rows: list[dict], corpus: str | None = None) -> dict[str, dict]:
    subsets = CORPUS_SUBSETS[corpus or active_corpus()]
    out = {}
    for subset in subsets:
        whole = subset == subsets[-1]
        chosen = rows if whole else [r for r in rows if r["subset"] == subset]
        if chosen:
            out[subset] = summarize(chosen)
    return out


def _clean_ranking(raw: object, example_id: str) -> list[str]:
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        raise TypeError(f"{example_id}: parent_id 목록(list[str])을 돌려줘야 합니다: {type(raw)}")
    if not all(isinstance(p, str) for p in raw):
        raise TypeError(f"{example_id}: parent_id는 문자열이어야 합니다")
    ranked = list(dict.fromkeys(raw))
    if len(ranked) > TOP_K:
        raise ValueError(f"{example_id}: 상위 {TOP_K}개까지만 돌려줍니다(받은 수 {len(ranked)})")
    return ranked


# ---- 실행 ----


async def run_method(
    method: str,
    retrieve: Retriever,
    examples: list[Example],
    *,
    max_model_calls: int | None = None,
    params: dict | None = None,
    description: str = "",
) -> dict:
    """모든 문항을 차례로 돌려 결과(JSON으로 쓸 사전)를 만든다. 예외는 감싸지 않고 멈춘다."""
    meter = Meter(max_calls=max_model_calls)
    rows = []
    for example in examples:
        question = example.question
        before_calls, before_in, before_out = meter.snapshot()
        before_stages = len(meter.stages)
        started = time.perf_counter()
        raw = retrieve(question, meter)
        if inspect.isawaitable(raw):
            raw = await raw
        seconds = time.perf_counter() - started
        ranked = _clean_ranking(raw, question.example_id)
        after_calls, after_in, after_out = meter.snapshot()
        by_model = {
            m: c - before_calls.get(m, 0)
            for m, c in after_calls.items()
            if c != before_calls.get(m, 0)
        }
        rows.append(
            {
                "example_id": question.example_id,
                "subset": example.subset,
                "question": question.text,
                "accident_date": question.accident_date,
                "expected": list(example.expected),
                "ranked": ranked,
                "scores": rank_scores(ranked, example.expected),
                "seconds": seconds,
                "model_calls": sum(by_model.values()),
                "model_calls_by_model": by_model,
                "input_tokens": None if after_in is None else after_in - (before_in or 0),
                "output_tokens": None if after_out is None else after_out - (before_out or 0),
                "stages": _score_stages(meter.stages[before_stages:], example.expected),
            }
        )
    return {
        "schema": SCHEMA,
        "method": method,
        "description": description,
        "created_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "git_commit": git_commit(),
        "dataset": dataset_info(examples),
        "corpus": corpus_info(),
        "common": common_settings(),
        "params": params or {},
        "metrics": subset_metrics(rows),
        "questions": rows,
    }


def git_commit() -> str | None:
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return done.stdout.strip() or None


def dataset_info(examples: list[Example], corpus: str | None = None) -> dict:
    corpus = corpus or active_corpus()
    subsets = CORPUS_SUBSETS[corpus]
    parts = subsets[:-1] if len(subsets) > 1 else subsets
    counts = {s: sum(e.subset == s for e in examples) for s in parts}
    info: dict[str, object] = {"corpus": corpus, "count": len(examples), "subsets": counts}
    path = QUESTIONS if corpus == CORPUS else CORPUS_QUESTIONS[corpus]
    if path.is_file():
        info["sha256"] = file_sha256(path)
    return info


def corpus_info(base: Path = DB_IMPORT) -> dict:
    """db_import 파일별 sha256(같은 코퍼스로 돌렸는지 확인용)."""
    if not base.is_dir():
        return {}
    return {p.name: file_sha256(p) for p in sorted(base.glob("*.jsonl"))}


def save_result(result: dict, out_dir: Path = RESULTS) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"{result['method']}-{stamp}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load_result(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        raise ValueError(f"{path}: {SCHEMA} 결과가 아닙니다")
    return data


def format_table(results: list[dict], subset: str) -> str:
    """결과 여러 개를 마크다운 표 하나로."""
    head = (
        "| 방식 | 문항 | Top-1 | R@3 | R@5 | R@10 | MRR@10 | 질문당 초(평균/p95) "
        "| 질문당 호출 | 입력/출력 토큰 | 단계 생존율 |"
    )
    lines = [head, "| " + " | ".join(["---"] * 11) + " |"]
    for result in results:
        m = result["metrics"].get(subset)
        if not m:
            continue
        tokens = (
            "-"
            if m["input_tokens"] is None
            else f"{m['input_tokens']:,} / {m['output_tokens'] or 0:,}"
        )
        survival = m.get("stage_survival")
        stages = " · ".join(f"{k} {v:.3f}" for k, v in survival.items()) if survival else "-"
        lines.append(
            f"| {result['method']} | {m['n']} | {m['top1']:.3f} | {m['r3']:.3f} | {m['r5']:.3f} "
            f"| {m['r10']:.3f} | {m['mrr10']:.3f} | {m['seconds_mean']:.3f} / "
            f"{m['seconds_p95']:.3f} | {m['model_calls_per_question']:.1f} | {tokens} | {stages} |"
        )
    return "\n".join(lines)
