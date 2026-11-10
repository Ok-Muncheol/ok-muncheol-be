"""D 시험: 실제 모델(.env의 OPENAI_MODEL)로 llm_judge를 돌려 면책 판정을 확인한다. CI 제외.

실행: py -3.12 scripts/try_exclusion_judge.py          (판정기만, 최대 4번)
      py -3.12 scripts/try_exclusion_judge.py --final  (무면허·음주 True 최종 결과, 최대 4번)
      py -3.12 scripts/try_exclusion_judge.py --final --max-calls 2  (무면허만, 최대 2번)
준비: py -3.12 -m pip install -e ".[embedding]", .env에 OPENAI_API_KEY, OPENAI_MODEL
- 합성 사건 4개, 근거는 제23조 직접 조회뿐 = LLM 호출 최대 4번. 재시도 없음.
- 사실 입력은 운영과 같게 case_facts의 무면허·음주·고의만 넘긴다.
- "해당 없음"은 호출 상한 때문에 무면허 주제 하나로만 시험한다.
- 약관 금액·판정을 확정하는 시험이 아니다. 키 값은 출력하지 않는다.
"""

import argparse
import asyncio
import dataclasses
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

from app.api import reports as api  # noqa: E402
from app.calculation.exclusions import (  # noqa: E402
    fetch_exclusion_clauses,
    llm_judge,
    openai_judge_llm,
)
from app.retrieval.common import configure_retrieval, fetch_by_parent  # noqa: E402
from app.retrieval.embedding import (  # noqa: E402
    EmbeddingMismatch,
    EmbeddingRequestFailed,
    RetrievalNotConfigured,
)
from app.retrieval.loader import handoff_terms_edition, load_index  # noqa: E402
from tests import fixtures_d as fx  # noqa: E402

MAX_CALLS = 4
MAX_FINAL_CALLS = 4
NO_FLAGS = {"무면허": False, "음주": False, "고의": False}
CASES = [
    ("무면허=True", "무면허 운전 면책", {**NO_FLAGS, "무면허": True}, "fail", "14"),
    ("음주=True", "음주 운전 자기차량손해 면책", {**NO_FLAGS, "음주": True}, "fail", "14"),
    ("고의=True", "고의 사고 면책", {**NO_FLAGS, "고의": True}, "fail", "1"),
    ("해당 없음", "무면허 운전 면책", NO_FLAGS, "pass", None),
]


class LimitReached(RuntimeError):
    pass


class CountingLLM:
    """호출 수를 세고 상한을 넘으면 멈춘다."""

    def __init__(self, llm, limit=MAX_CALLS):
        self.llm = llm
        self.limit = limit
        self.calls = 0

    def __call__(self, prompt: str) -> str:
        if self.calls >= self.limit:
            raise LimitReached(f"LLM 호출 상한 {self.limit}번")
        self.calls += 1
        return self.llm(prompt)


def cites_item(detail: str, item: str | None) -> str:
    """판정 이유에 기대한 호 번호가 있는지. 조항 ID로는 호를 구분할 수 없어 문구로 본다."""
    if item is None:
        return "-"
    return "예" if f"{item}호" in detail.replace(" ", "") else "아니요"


async def main() -> None:
    configure_retrieval(
        index=load_index(["policy_terms"]), default_terms_edition=handoff_terms_edition()
    )
    llm = CountingLLM(openai_judge_llm(max_retries=0))
    judge = llm_judge(llm)
    print(f"모델: {os.environ['OPENAI_MODEL']} · 호출 상한 {MAX_CALLS}번")
    print("| 사건 | 근거 수 | 기대 | 판정 | 호 인용 | 인용 조항 ID | 이유 |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for name, topic, facts, expected, item in CASES:
        hits = await fetch_exclusion_clauses(topic, edition=None, fetch=fetch_by_parent)
        check = judge(topic, hits, facts)
        ids = ", ".join(i.split(":", 1)[1] for i in check["clause_ids"]) or "-"
        detail = " ".join(check["detail"].split())
        print(
            f"| {name} | {len(hits)} | {expected} | {check['result']} | "
            f"{cites_item(detail, item)} | {ids} | {detail} |",
            flush=True,
        )
    print(f"\nLLM 호출 수: {llm.calls}번")


async def final(max_calls: int) -> None:
    """운영 연결로 끝까지 돌려 최종 결과를 본다. 호출 수를 지키려고 주제를 사건당 하나로 줄인다.

    사건마다 LLM 2번: 판단 반영(입력 없음), 서류·입력 추가(True).
    """
    api.configure_with_knowledge_base()
    llm = CountingLLM(openai_judge_llm(max_retries=0), max_calls)
    print(f"모델: {os.environ['OPENAI_MODEL']} · 호출 상한 {max_calls}번")
    for name, topic, facts, _, _ in CASES[: max_calls // 2]:
        rules = dataclasses.replace(api.services.rules, exclusion_topics=(topic,))
        api.configure(repo=api.InMemoryReportRepository(), rules=rules, judge=llm_judge(llm))
        v1 = await api.submit_judgment("handler-1", fx.card(), fx.judgment())
        report = await api.add_documents(
            "handler-1", "synthetic-case", [fx.estimate()], case_facts=facts
        )
        calc = report["calculation"]
        before = next(c for c in v1["calculation"]["checks"] if c["label"] == topic)
        check = next(c for c in calc["checks"] if c["label"] == topic)
        out = await api.confirm_report(
            "handler-1", "synthetic-case", report["versions"]["report_version"]
        )
        print(f"\n[{name}]")
        print(f"  입력 없음(v1) LLM 판정: {before['result']} · {before['detail']}")
        print(f"  검증 행: {check['result']} · {check['decided_by']} · {check['detail']}")
        print(f"  인용 조항: {check['clause_ids']}")
        print(f"  산정 상태: {calc['status']} · 보험금: {calc['insurance_payment']}")
        print(f"  산정 사유: {calc['reasons']}")
        print(f"  청구서: {out['package'] is not None} · 사유: {out['claim_blocked_reasons']}")
        sources = await fetch_exclusion_clauses(topic, edition=None, fetch=fetch_by_parent)
        print("  판정기 근거(순서대로):")
        for hit in sources:
            chunk = hit["chunk"]
            role = chunk["metadata"].get("retrieval_role")
            print(f"    {chunk['parent_id'].split(':', 1)[1]} · p.{chunk['page_start']} · {role}")
    print(f"\nLLM 호출 수: {llm.calls}번")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="실제 모델로 면책 판정 확인")
    parser.add_argument("--final", action="store_true", help="무면허·음주 True 최종 결과")
    parser.add_argument("--max-calls", type=int, default=MAX_FINAL_CALLS, choices=(2, 4))
    arguments = parser.parse_args()
    try:
        asyncio.run(final(arguments.max_calls) if arguments.final else main())
    except (
        RetrievalNotConfigured,
        EmbeddingMismatch,
        EmbeddingRequestFailed,
        LimitReached,
    ) as exc:
        raise SystemExit(f"실행 불가: {exc}") from None
