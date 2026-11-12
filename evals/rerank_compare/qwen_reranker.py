"""D: Qwen3-Reranker-0.6B 점수기(실험용, 로컬). torch·transformers는 운영 의존성이 아니다.

설치(저장소 밖 가상환경): evals/rerank_compare/requirements-qwen.txt.
입력은 common의 qwen_prompt와 같은 글이다(QWEN_PREFIX + relevance_user_text + QWEN_SUFFIX).
모델 카드처럼 세 부분을 따로 토큰화해 이어 붙이고, 마지막 위치의 "yes"·"no" 로짓을 softmax해
P(yes)를 얻는다(Yes·No 둘로만 정규화 — 논문 식 2와 같다). 글은 하나씩 따로 판단한다(배치는 속도용).
"""

from collections.abc import Sequence

from evals.rerank_compare.common import (
    QWEN_PREFIX,
    QWEN_RERANKER_MODEL,
    QWEN_SUFFIX,
    qwen_prompt,
    relevance_user_text,
)

# Hugging Face 커밋(재현용으로 고정). 바꾸면 결과를 다시 돌린다
QWEN_RERANKER_REVISION = "e61197ed45024b0ed8a2d74b80b4d909f1255473"
MAX_LENGTH = 8192  # 모델 카드 값. 넘으면 자르지 않고 멈춘다
BATCH_SIZE = 16


class QwenReranker:
    model_name = "Qwen3-Reranker-0.6B"

    def __init__(
        self,
        *,
        model: str = QWEN_RERANKER_MODEL,
        revision: str = QWEN_RERANKER_REVISION,
        device: str | None = None,
        batch_size: int = BATCH_SIZE,
    ) -> None:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self._torch = torch
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.dtype = torch.float16 if self.device == "cuda" else torch.float32
        self.batch_size = batch_size
        self.revision = revision
        self.tokenizer = AutoTokenizer.from_pretrained(
            model, revision=revision, padding_side="left"
        )
        self.model = (
            AutoModelForCausalLM.from_pretrained(model, revision=revision, dtype=self.dtype)
            .to(self.device)
            .eval()
        )
        self.yes_id = self.tokenizer.convert_tokens_to_ids("yes")
        self.no_id = self.tokenizer.convert_tokens_to_ids("no")
        self._prefix = self.tokenizer.encode(QWEN_PREFIX, add_special_tokens=False)
        self._suffix = self.tokenizer.encode(QWEN_SUFFIX, add_special_tokens=False)

    def describe(self) -> dict:
        torch = self._torch
        return {
            "model": QWEN_RERANKER_MODEL,
            "revision": self.revision,
            "device": self.device,
            "device_name": torch.cuda.get_device_name(0) if self.device == "cuda" else "cpu",
            "dtype": str(self.dtype).replace("torch.", ""),
            "batch_size": self.batch_size,
            "torch": torch.__version__,
            "transformers": __import__("transformers").__version__,
            "score": "softmax(logit[no], logit[yes])[yes] at last position",
        }

    def encode(self, question: str, text: str) -> list[int]:
        """모델 카드 방식 토큰(접두 + 본문 + 접미). 글은 qwen_prompt와 같다."""
        body = self.tokenizer.encode(relevance_user_text(question, text), add_special_tokens=False)
        ids = self._prefix + body + self._suffix
        if len(ids) > MAX_LENGTH:
            raise ValueError(f"입력이 {MAX_LENGTH}토큰을 넘습니다: {len(ids)}")
        return ids

    def score(self, question: str, texts: Sequence[str]) -> tuple[list[float], int, int]:
        torch = self._torch
        encoded = [self.encode(question, t) for t in texts]
        order = sorted(range(len(encoded)), key=lambda i: -len(encoded[i]))  # 비슷한 길이끼리
        scores = [0.0] * len(encoded)
        with torch.inference_mode():
            for start in range(0, len(order), self.batch_size):
                batch = order[start : start + self.batch_size]
                padded = self.tokenizer.pad(
                    {"input_ids": [encoded[i] for i in batch]}, padding=True, return_tensors="pt"
                ).to(self.device)
                # 마지막 위치 로짓만(전체 어휘 × 전체 위치를 만들면 8GB GPU에서 OOM)
                logits = self.model(**padded, logits_to_keep=1).logits[:, -1, :]
                pair = torch.stack([logits[:, self.no_id], logits[:, self.yes_id]], dim=1)
                probs = torch.softmax(pair.float(), dim=1)[:, 1].tolist()
                for i, p in zip(batch, probs, strict=True):
                    scores[i] = p
        return scores, sum(len(ids) for ids in encoded), 0  # 생성하지 않으므로 출력 0


def check_prompt_matches(question: str, text: str) -> bool:
    """세 부분을 이은 글이 common.qwen_prompt와 같은지(테스트용)."""
    joined = QWEN_PREFIX + relevance_user_text(question, text) + QWEN_SUFFIX
    return joined == qwen_prompt(question, text)
