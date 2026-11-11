"""C: ③ 현재 RAG + Qwen3-Reranker, ④ 현재 RAG + GPT 재정렬. 김지은 담당.

후보 parent 50개(RRF)의 모든 청크 → 청크마다 "관련 있나?" Yes 확률 → MaxP → 상위 10개.
문구·넣는 글·자르기·후보 수·GPT 요청·Yes 확률·MaxP는 모두 common.py 것만 쓴다.

    python -m evals.rerank_compare.run \
        --retriever evals.rerank_compare.current_rag_rerank:build_qwen \
        --name qwen-rerank-rrf50 --max-model-calls 9000
    python -m evals.rerank_compare.run \
        --retriever evals.rerank_compare.current_rag_rerank:build_gpt \
        --name gpt-rerank-rrf50 --max-model-calls 9000

호출 수는 청크(모델 판단) 하나당 1회로 센다. Qwen은 배치로 돌리지만 판단 수는 같다.
"""

import asyncio

from evals.rerank_compare.common import (
    CANDIDATE_MODE,
    CANDIDATE_PARENTS,
    GPT_MODEL,
    QWEN_RERANKER_MODEL,
    TOP_K,
    gpt_request,
    max_p,
    qwen_prompt,
    yes_probability,
)

GPT_CONCURRENCY = 8  # 질문 하나의 청크를 동시에 몇 개 부를지(결과에는 영향 없음)
QWEN_BATCH = 16


def _params(**extra: object) -> dict[str, object]:
    return {"candidates": CANDIDATE_MODE, "candidate_parents": CANDIDATE_PARENTS, **extra}


class QwenReranker:
    """모델 카드 방식: 답 자리의 "yes"·"no" 로짓 softmax에서 yes 확률."""

    def __init__(self, batch_size: int = QWEN_BATCH) -> None:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.torch = torch
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        dtype = torch.float16 if self.device == "cuda" else torch.float32
        self.tokenizer = AutoTokenizer.from_pretrained(QWEN_RERANKER_MODEL, padding_side="left")
        self.model = AutoModelForCausalLM.from_pretrained(QWEN_RERANKER_MODEL, dtype=dtype)
        self.model.to(self.device).eval()
        self.yes = self.tokenizer.convert_tokens_to_ids("yes")
        self.no = self.tokenizer.convert_tokens_to_ids("no")
        self.batch_size = batch_size

    def score(self, prompts: list[str]) -> tuple[list[float], int]:
        """(yes 확률들, 입력 토큰 수). 글은 common에서 512토큰으로 잘려 와서 더 자르지 않는다."""
        torch = self.torch
        inputs = self.tokenizer(
            prompts, add_special_tokens=False, padding=True, return_tensors="pt"
        ).to(self.device)
        with torch.no_grad():
            logits = self.model(**inputs).logits[:, -1, :]
        pair = torch.stack([logits[:, self.no], logits[:, self.yes]], dim=1).float()
        tokens = int(inputs["attention_mask"].sum())
        return torch.softmax(pair, dim=1)[:, 1].tolist(), tokens


def build_qwen(search):
    reranker = QwenReranker()

    async def retrieve(question, meter):
        passages = await search.candidate_passages(question, mode=CANDIDATE_MODE)
        scored: list[tuple[str, float]] = []
        for i in range(0, len(passages), reranker.batch_size):
            batch = passages[i : i + reranker.batch_size]
            meter.add(QWEN_RERANKER_MODEL, calls=len(batch))
            prompts = [qwen_prompt(question.text, p.text) for p in batch]
            scores, tokens = await asyncio.to_thread(reranker.score, prompts)
            meter.add(QWEN_RERANKER_MODEL, calls=0, input_tokens=tokens, output_tokens=0)
            scored += [(p.parent_id, s) for p, s in zip(batch, scores, strict=True)]
        return [parent_id for parent_id, _ in max_p(scored)[:TOP_K]]

    retrieve.params = _params(  # type: ignore[attr-defined]
        model=QWEN_RERANKER_MODEL,
        device=reranker.device,
        batch_size=reranker.batch_size,
        score="softmax(no, yes) at answer position",
        calls_counted="per chunk",
    )
    return retrieve


def build_gpt(search):
    from dotenv import load_dotenv
    from openai import AsyncOpenAI

    load_dotenv(override=False)
    client = AsyncOpenAI()
    limit = asyncio.Semaphore(GPT_CONCURRENCY)

    async def judge(question, passage, meter) -> tuple[str, float]:
        async with limit:
            response = await client.chat.completions.create(
                **gpt_request(question.text, passage.text)
            )
        usage = response.usage
        meter.add(
            GPT_MODEL,
            calls=0,
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
        )
        top = response.choices[0].logprobs.content[0].top_logprobs
        return passage.parent_id, yes_probability(top)

    async def retrieve(question, meter):
        passages = await search.candidate_passages(question, mode=CANDIDATE_MODE)
        for _ in passages:  # 돈을 쓰기 전에 이 질문의 호출 수를 모두 센다(상한이면 여기서 멈춤)
            meter.add(GPT_MODEL)
        scored = await asyncio.gather(*(judge(question, p, meter) for p in passages))
        return [parent_id for parent_id, _ in max_p(scored)[:TOP_K]]

    retrieve.params = _params(  # type: ignore[attr-defined]
        model=GPT_MODEL,
        concurrency=GPT_CONCURRENCY,
        calls_counted="per chunk",
    )
    return retrieve
