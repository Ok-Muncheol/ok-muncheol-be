"""C: ③ 현재 RAG + Qwen3-Reranker, ④ 현재 RAG + GPT 재정렬. 김지은 담당.

후보 parent 50개(RRF)의 모든 청크 → 청크마다 "관련 있나?" Yes 확률 → MaxP → 상위 10개.
문구·넣는 글·자르기·후보 수·GPT 요청·Yes 확률·MaxP는 모두 common.py 것만 쓴다.
점수기는 ①②와 같은 것을 쓴다: ③ qwen_reranker.QwenReranker(revision 고정), ④ gpt_judge.GptJudge.

    python -m evals.rerank_compare.run \
        --retriever evals.rerank_compare.current_rag_rerank:build_qwen \
        --name qwen-rerank-rrf50 --max-model-calls 9000
    python -m evals.rerank_compare.run \
        --retriever evals.rerank_compare.current_rag_rerank:build_gpt \
        --name gpt-rerank-rrf50 --max-model-calls 9000

호출 수는 청크(모델 판단) 하나당 1회로 세고, 부르기 전에 센다(상한이면 돈·시간을 쓰기 전에 멈춤).
후보 방식은 기본으로 search.candidate_mode(코퍼스별 common.CANDIDATE_MODES, 지금은 모두 rrf)를
따른다. 바꿔 돌릴 때는 팩토리를 감싼다: partial(build_qwen, mode="bm25"). 실제로 쓴 값은 결과 JSON
params.candidates에 남는다.
"""

import inspect

from evals.rerank_compare.common import CANDIDATE_PARENTS, TOP_K, max_p


class CurrentRagReranker:
    """후보 청크 전부를 scorer로 판단하고 MaxP로 parent 순위를 낸다.

    scorer는 model_name과 score(question, texts) -> (점수들, 입력 토큰, 출력 토큰)을 가진다
    (QwenReranker·GptJudge, score는 동기·비동기 모두 된다).
    """

    def __init__(self, search, scorer, mode: str | None = None) -> None:
        self.search = search
        self.scorer = scorer
        self.mode = mode or search.candidate_mode  # 기본은 코퍼스별 설정을 따른다(덮지 않음)
        self.params: dict = {"candidates": self.mode, "candidate_parents": CANDIDATE_PARENTS}

    async def __call__(self, question, meter) -> list[str]:
        passages = await self.search.candidate_passages(question, mode=self.mode)
        if not passages:
            return []
        name = self.scorer.model_name
        meter.add(name, calls=len(passages))  # 부르기 전에 센다(상한)
        result = self.scorer.score(question.text, [p.text for p in passages])
        if inspect.isawaitable(result):
            result = await result
        scores, input_tokens, output_tokens = result
        if len(scores) != len(passages):
            raise RuntimeError(f"점수 수 {len(scores)} ≠ 글 수 {len(passages)}")
        meter.add(name, calls=0, input_tokens=input_tokens, output_tokens=output_tokens)
        ranked = max_p((p.parent_id, s) for p, s in zip(passages, scores, strict=True))
        return [parent_id for parent_id, _ in ranked[:TOP_K]]


def build_qwen(search, mode: str | None = None):
    """③: ①과 같은 점수기(qwen_reranker.QwenReranker)."""
    import asyncio

    from evals.rerank_compare.qwen_reranker import QwenReranker

    reranker = QwenReranker()

    class _Threaded:  # GPU 계산이 이벤트 루프를 막지 않게
        model_name = reranker.model_name

        async def score(self, question, texts):
            return await asyncio.to_thread(reranker.score, question, texts)

    retriever = CurrentRagReranker(search, _Threaded(), mode)
    retriever.params |= {"scorer": reranker.describe(), "calls_counted": "per chunk"}
    return retriever


def build_gpt(search, mode: str | None = None):
    """④: ②와 같은 점수기(gpt_judge.GptJudge). 실제 사용량·비용은 실행 뒤 params.usage에."""
    from evals.rerank_compare.gpt_judge import GptJudge

    judge = GptJudge()
    retriever = CurrentRagReranker(search, judge, mode)
    retriever.params |= {"scorer": judge.describe(), "calls_counted": "per chunk"}
    retriever.after_run = lambda: {"usage": judge.usage()}  # run.py가 실행 뒤 params에 더한다
    return retriever
