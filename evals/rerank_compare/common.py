"""D: 재정렬·판단 모델(①~④)이 같이 쓰는 상수와 함수. 김지은 님과 정한 공통 조건이다.

- 모델에 넣는 글: "조항 제목 + 본문", 본문은 o200k_base 512토큰으로 자른다(passage_text)
- 같은 parent_id의 청크 여러 개 → 최고 점수로 합친다(max_p, MaxP)
- "관련 있나?" 문구는 RELEVANCE_INSTRUCTION 하나. Qwen3-Reranker와 GPT가 같은 글을 받는다
- GPT: gpt-4o-mini, temperature 0, logprobs로 Yes 확률(yes_probability)

평가 80문항을 보고 문구·자르기 길이를 고치지 않는다. 바꾸면 PROMPT_VERSION을 올리고 모든 방식을
다시 돌린다. 이 파일은 API를 부르지 않는다(호출은 각 방식 구현 쪽).
"""

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

CORPUS = "policy_terms"
TOP_K = 10  # 끼우는 함수가 돌려줄 parent 수
CANDIDATE_PARENTS = 50  # ③④ 재정렬 후보: 1단계 검색의 고유 parent 50개
# ③④ 후보를 뽑는 1단계 검색(11/11 결정). 약관 80 R@50: RRF 1.000, BM25 0.975
CANDIDATE_MODE = "rrf"

# gpt-4o-mini 토크나이저. Qwen에도 이 기준으로 자른 같은 글을 넣는다
TOKEN_ENCODING = "o200k_base"
MAX_PASSAGE_TOKENS = 512  # 본문만 센다(제목 제외)

PROMPT_VERSION = "relevance-v1"
# Qwen3-Reranker 모델 카드의 system 문장. GPT에도 같은 system을 준다(두 모델 문구를 최대한 같게)
SYSTEM_PROMPT = (
    "Judge whether the Document meets the requirements based on the Query and the Instruct "
    'provided. Note that the answer can only be "yes" or "no".'
)
RELEVANCE_INSTRUCTION = (
    "Given a question about an auto insurance claim, judge whether this policy clause provides "
    "the basis needed to answer the question."
)
# GPT에만 덧붙인다(Qwen은 yes/no 토큰 확률을 바로 읽는다)
ANSWER_INSTRUCTION = "Answer only Yes or No."

QWEN_RERANKER_MODEL = "Qwen/Qwen3-Reranker-0.6B"
# Qwen3-Reranker 모델 카드의 대화 틀(system·user 사이, 답 앞). 모델 고유 형식이라 그대로 쓴다
QWEN_PREFIX = f"<|im_start|>system\n{SYSTEM_PROMPT}<|im_end|>\n<|im_start|>user\n"
QWEN_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"

GPT_MODEL = "gpt-4o-mini"
GPT_TEMPERATURE = 0
GPT_MAX_TOKENS = 1
GPT_TOP_LOGPROBS = 5


def common_settings() -> dict[str, object]:
    """결과 JSON에 그대로 남기는 공통 조건(나중에 같은 조건으로 돌렸는지 비교용)."""
    return {
        "corpus": CORPUS,
        "top_k": TOP_K,
        "candidate_parents": CANDIDATE_PARENTS,
        "candidate_mode": CANDIDATE_MODE,
        "token_encoding": TOKEN_ENCODING,
        "max_passage_tokens": MAX_PASSAGE_TOKENS,
        "prompt_version": PROMPT_VERSION,
        "system_prompt": SYSTEM_PROMPT,
        "relevance_instruction": RELEVANCE_INSTRUCTION,
        "answer_instruction": ANSWER_INSTRUCTION,
        "qwen_reranker_model": QWEN_RERANKER_MODEL,
        "gpt_model": GPT_MODEL,
        "gpt_temperature": GPT_TEMPERATURE,
        "gpt_top_logprobs": GPT_TOP_LOGPROBS,
    }


# ---- 글 자르기 ----


class TokenEncoder(Protocol):
    def encode(self, text: str) -> list[int]: ...

    def decode(self, tokens: list[int]) -> str: ...


_encoder: TokenEncoder | None = None


def token_encoder() -> TokenEncoder:
    """o200k_base 인코더(처음 한 번 읽는다). tiktoken은 처음 쓸 때 BPE 파일을 받을 수 있다."""
    global _encoder
    if _encoder is None:
        import tiktoken

        _encoder = tiktoken.get_encoding(TOKEN_ENCODING)
    return _encoder


def truncate_tokens(
    text: str, max_tokens: int = MAX_PASSAGE_TOKENS, *, encoder: TokenEncoder | None = None
) -> str:
    """앞에서부터 max_tokens 토큰까지. 글자 중간에서 잘려 생긴 깨진 문자는 뗀다."""
    encoder = encoder or token_encoder()
    tokens = encoder.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return encoder.decode(tokens[:max_tokens]).rstrip("�")


def passage_text(title: str, body: str, *, encoder: TokenEncoder | None = None) -> str:
    """모델에 넣는 글: 제목 한 줄 + 본문(512토큰).

    본문 첫머리의 제목 줄은 뺀다. "대인배상Ⅰ | 제3조(…)"처럼 담보 이름이 붙은 제목 줄이면 그 줄을
    제목으로 쓴다(담보 이름은 남기고 같은 제목이 여러 번 들어가지 않게).
    """
    title = title.strip()
    lines = body.strip().split("\n")
    base = title
    while lines and base:
        first = lines[0].strip()
        if first == base:
            lines.pop(0)
        elif first.endswith(f"| {base}") and title == base:
            title = first
            lines.pop(0)
        else:
            break
    body = truncate_tokens("\n".join(lines).strip(), encoder=encoder)
    return f"{title}\n{body}" if title else body


# ---- 모델 입력 ----


def relevance_user_text(question: str, passage: str) -> str:
    """두 모델 공통 user 글(Qwen3-Reranker 형식)."""
    return f"<Instruct>: {RELEVANCE_INSTRUCTION}\n<Query>: {question}\n<Document>: {passage}"


def qwen_prompt(question: str, passage: str) -> str:
    """Qwen3-Reranker에 넣는 전체 글. 점수는 다음 토큰 "yes"·"no" 확률로 읽는다."""
    return QWEN_PREFIX + relevance_user_text(question, passage) + QWEN_SUFFIX


def gpt_request(question: str, passage: str) -> dict[str, Any]:
    """OpenAI chat.completions.create 인자. 호출은 하지 않는다."""
    return {
        "model": GPT_MODEL,
        "temperature": GPT_TEMPERATURE,
        "max_tokens": GPT_MAX_TOKENS,
        "logprobs": True,
        "top_logprobs": GPT_TOP_LOGPROBS,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"{relevance_user_text(question, passage)}\n{ANSWER_INSTRUCTION}",
            },
        ],
    }


def _token_logprob(item: object) -> tuple[str, float]:
    if isinstance(item, Mapping):
        return str(item["token"]), float(item["logprob"])
    if isinstance(item, tuple):
        return str(item[0]), float(item[1])
    return str(item.token), float(item.logprob)  # type: ignore[attr-defined]


def yes_probability(top_logprobs: Iterable[object]) -> float:
    """첫 답 토큰의 top_logprobs에서 Yes 확률. 대소문자·앞뒤 공백이 다른 "yes"는 합친다.

    정규화하지 않은 P(yes)다(No와 나누지 않음). top_logprobs에 없으면 0.0.
    항목은 OpenAI 응답 객체(.token·.logprob), {"token", "logprob"} 사전, (token, logprob)
    모두 받는다.
    """
    total = 0.0
    for item in top_logprobs:
        token, logprob = _token_logprob(item)
        if token.strip().lower() == "yes":
            total += math.exp(logprob)
    return min(total, 1.0)


# ---- MaxP ----


def max_p(chunk_scores: Iterable[tuple[str, float]]) -> list[tuple[str, float]]:
    """(parent_id, 청크 점수) → parent별 최고 점수, 점수 내림차순. 동점은 parent_id 순(결정적)."""
    best: dict[str, float] = {}
    for parent_id, score in chunk_scores:
        if parent_id not in best or score > best[parent_id]:
            best[parent_id] = score
    return sorted(best.items(), key=lambda item: (-item[1], item[0]))


# ---- 조항 글 ----


@dataclass(frozen=True)
class Passage:
    parent_id: str
    chunk_id: str
    text: str  # passage_text 결과(제목 + 512토큰 본문)


def _record_title(body: Mapping[str, Any]) -> str:
    if body.get("title"):
        return str(body["title"]).strip()
    if body.get("schedule"):
        parts = [body.get("schedule"), body.get("schedule_title"), body.get("item")]
        return " ".join(str(p).strip() for p in parts if p)
    return ""


class PolicyParents:
    """약관 parent별 제목·청크(페이지 순). corpus는 app.retrieval.loader.read_corpus() 결과."""

    def __init__(self, corpus: Any, *, encoder: TokenEncoder | None = None) -> None:
        self._encoder = encoder
        self._chunks: dict[str, list[dict]] = {}
        for chunk in corpus.chunks:
            if chunk["corpus"] == CORPUS:
                self._chunks.setdefault(chunk["parent_id"], []).append(chunk)
        for chunks in self._chunks.values():
            chunks.sort(key=lambda c: c["page_start"])  # 같은 페이지는 파일 순서
        self._titles: dict[str, str] = {}
        for record in corpus.records:
            if record.get("corpus") == CORPUS and record.get("is_parent"):
                title = _record_title(record["body"])
                if title:
                    self._titles[record["parent_id"]] = title

    def parent_ids(self) -> list[str]:
        return sorted(self._chunks)

    def chunks(self, parent_id: str) -> list[dict]:
        return list(self._chunks.get(parent_id, []))

    def title(self, parent_id: str) -> str:
        """레코드 title → 별표 schedule·schedule_title·item → 첫 청크 첫 줄."""
        if parent_id in self._titles:
            return self._titles[parent_id]
        chunks = self._chunks.get(parent_id)
        return chunks[0]["text"].strip().split("\n", 1)[0].strip() if chunks else ""

    def passages(self, parent_id: str) -> list[Passage]:
        """parent의 청크마다 하나씩(모델 입력). MaxP로 합칠 때 쓴다."""
        title = self.title(parent_id)
        return [
            Passage(parent_id, c["chunk_id"], passage_text(title, c["text"], encoder=self._encoder))
            for c in self._chunks.get(parent_id, [])
        ]
