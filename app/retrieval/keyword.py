"""D: 키워드 검색(BM25)과 한국어 토큰화. 코퍼스별 통계로 계산한다(문서 빈도가 코퍼스마다 다르다).

토큰화는 Kiwi(kiwipiepy, 선택 의존성)를 쓰고 없으면 간단 토큰화로 대체한다. 색인과 질의는 같은
토큰화를 써야 하므로 색인이 토큰화 객체를 갖고 있고, 이름은 retrieval_version에 남긴다.
"""

import math
import re
from collections import Counter
from functools import lru_cache
from typing import Protocol

K1 = 1.5
B = 0.75
_WORD = re.compile(r"[가-힣]+|[a-z0-9]+")
_KIWI_TAGS = (
    "NN",
    "VV",
    "VA",
    "XR",
    "SL",
    "SH",
    "SN",
)  # 명사, 동사·형용사 어간, 어근, 외국어, 숫자


class Tokenizer(Protocol):
    name: str

    def tokenize(self, text: str) -> list[str]: ...


class SimpleTokenizer:
    """외부 패키지 없는 대체 토큰화. 영문·숫자는 단어로, 한글은 2글자 조각으로 나눈다."""

    name = "simple"

    def tokenize(self, text: str) -> list[str]:
        tokens: list[str] = []
        for word in _WORD.findall(text.lower()):
            if word[0].isascii() or len(word) == 1:
                tokens.append(word)
            else:
                tokens += [word[i : i + 2] for i in range(len(word) - 1)]
        return tokens


class KiwiTokenizer:
    """Kiwi 형태소 분석. 조사·어미를 빼고 내용어만 쓴다."""

    name = "kiwi"

    def __init__(self) -> None:
        from kiwipiepy import Kiwi

        self._kiwi = Kiwi()

    def tokenize(self, text: str) -> list[str]:
        return [
            token.form.lower()
            for token in self._kiwi.tokenize(text)
            if token.tag.startswith(_KIWI_TAGS)
        ]


@lru_cache(maxsize=1)
def default_tokenizer() -> Tokenizer:
    """Kiwi가 있으면 Kiwi, 없으면 간단 토큰화. Kiwi 모델은 한 번만 읽는다."""
    try:
        return KiwiTokenizer()
    except ImportError:
        return SimpleTokenizer()


class BM25Index:
    """한 코퍼스의 BM25 통계. 문서를 넣을 때 미리 세어 두고, 질의 때는 점수만 계산한다."""

    def __init__(self, tokenizer: Tokenizer) -> None:
        self._tokenizer = tokenizer
        self._counts: dict[str, Counter[str]] = {}
        self._lengths: dict[str, int] = {}
        self._document_frequency: Counter[str] = Counter()
        self._postings: dict[str, set[str]] = {}
        self._total_length = 0

    def __len__(self) -> int:
        return len(self._counts)

    def _remove(self, doc_id: str) -> None:
        counts = self._counts.pop(doc_id)
        self._total_length -= self._lengths.pop(doc_id)
        for term in counts:
            self._document_frequency[term] -= 1
            self._postings[term].discard(doc_id)

    def add(self, doc_id: str, text: str) -> None:
        """같은 doc_id를 다시 넣으면 교체한다."""
        if doc_id in self._counts:
            self._remove(doc_id)
        counts = Counter(self._tokenizer.tokenize(text))
        self._counts[doc_id] = counts
        self._lengths[doc_id] = sum(counts.values())
        self._total_length += self._lengths[doc_id]
        for term in counts:
            self._document_frequency[term] += 1
            self._postings.setdefault(term, set()).add(doc_id)

    def scores(self, query: str) -> dict[str, float]:
        """질의 단어가 하나라도 있는 문서의 점수. 없는 문서는 결과에 없다."""
        count = len(self._counts)
        if not count:
            return {}
        average_length = self._total_length / count
        out: dict[str, float] = {}
        for term in dict.fromkeys(self._tokenizer.tokenize(query)):
            frequency = self._document_frequency[term]
            if not frequency:  # 질의 단어가 있으면 문서 길이 합도 0보다 크다
                continue
            idf = math.log(1 + (count - frequency + 0.5) / (frequency + 0.5))
            for doc_id in self._postings[term]:
                tf = self._counts[doc_id][term]
                norm = K1 * (1 - B + B * self._lengths[doc_id] / average_length)
                out[doc_id] = out.get(doc_id, 0.0) + idf * tf * (K1 + 1) / (tf + norm)
        return out
