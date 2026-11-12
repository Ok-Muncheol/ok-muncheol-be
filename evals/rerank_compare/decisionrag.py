"""D: ① DecisionRAG 방식의 계층 탐색을 무료 오픈소스 모델로 재현(약관, 검색기 없음).

논문: Wan et al., "Retriever-Free Retrieval: Navigating Documents with Decision Models"
(arXiv 2610.35868). 약관 목차 트리를 위에서 아래로 내려가며 단계마다 판단 모델 점수로 상위 몇 개만
남기고(top-k 가지치기), 마지막에 남은 parent의 청크를 점수 매겨 MaxP로 상위 10개 parent를 돌려준다.

트리(TREE_VERSION): L1 묶음 28개(보통약관 장·절, 별표 1~5, 특약 Ⅰ~Ⅷ, 인용 법령) → L2 parent(조·특약·
별표 항목·각주·인용 법령 페이지) → L3 청크. 묶음은 원문 PDF 목차(p.45~51)의 제목과 조·특약 번호
범위다(코퍼스에 편·장 칸이 없음). 각주는 처음 연결한 조항(페이지 순)의 묶음에 넣는다.
예산(GROUP_BUDGET·PARENT_BUDGET)은 평가 문항을 보기 전에 트리 크기만 보고 정했다.
결과를 보고 바꾸지 않는다.
판단 모델은 점수기(Scorer)로 주입한다. ① build(search) = Qwen3-Reranker-0.6B,
② build_gpt(search) = gpt-4o-mini, ⑤ build_jev(search) = Jev(공유 문맥).
①②는 트리·예산·문구·하나씩 판단이 같고 점수기만 다르다. ⑤는 같은 트리·예산·노드 글·문구에
한 단계의 후보를 함께 판단한다(jev_judge.py).
"""

import inspect
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from evals.rerank_compare.common import (
    CORPUS,
    TOP_K,
    PolicyParents,
    TokenEncoder,
    max_p,
    passage_text,
)
from evals.rerank_compare.harness import Meter, Question

NAME = "DecisionRAG 방식의 계층 탐색을 무료 오픈소스 모델로 재현"
PAPER = (
    "Wan et al., Retriever-Free Retrieval: Navigating Documents with Decision Models, "
    "arXiv 2610.35868"
)
TREE_VERSION = "policy-toc-v1"
GROUP_BUDGET = 5  # L1: 묶음 28개 중 남길 수(약 18%)
PARENT_BUDGET = 20  # L2: 남길 parent 수(최종 10개의 두 배)
NODE_BODY_TOKENS = 128  # L2 노드 글에 넣는 본문 앞부분(o200k_base)

_COMMON_DEVIATIONS = (
    "계층: 약관 목차 구조(묶음 → 조항 → 청크)를 그대로 사용",
    # 조사하며 생긴 차이
    "다지선다 재정렬 단계 없음(Jev 기능)",
    "결과 단위: 논문은 문단 순위, 여기서는 parent 순위(청크 점수 MaxP, 평가 틀 공통)",
    "노드 설명: 논문은 제목·절 제목 또는 LLM 요약. 여기서는 묶음 = 경로 + 하위 조항 제목"
    "(인용 법령은 본문의 [법령명]), 조항 = 경로 + 제목 + 본문 앞 128토큰. LLM 요약 없음",
    "질문 문구: 논문은 단계별 질문, 여기서는 공통 RELEVANCE_INSTRUCTION 하나를 모든 단계에",
    "예산: 논문은 평가와 겹치지 않는 설계용 셋으로 고름. 여기서는 설계용 셋이 없어 "
    "트리 크기만 보고 평가 전에 고정(묶음 5, parent 20)",
    "확률 임계 규칙·대화 메모(notepad)·dense 후보 추가·페이지 점수 곱하기는 쓰지 않음",
    "편·장 정보가 코퍼스에 없어 원문 목차(p.45~51) 제목과 조·특약 번호 범위를 코드 상수로 옮김"
    "(목차의 제52조의2는 코퍼스에 없음)",
)
# ① Qwen3-Reranker-0.6B
DEVIATIONS = (
    "판단 모델: 논문의 Jev(유료 API) 대신 Qwen3-Reranker-0.6B(무료, 로컬)",
    "후보 판단: 논문은 한 단계의 후보를 한 문맥에서 함께 판단(공유 문맥), 여기서는 하나씩 판단"
    "(Qwen3-Reranker 구조상)",
    *_COMMON_DEVIATIONS,
)
# ② gpt-4o-mini — ①과 모델만 다르게
GPT_DEVIATIONS = (
    "판단 모델: 논문의 Jev 대신 gpt-4o-mini(유료 API, temperature 0, logprobs, max_tokens 1)",
    "후보 판단: 논문은 공유 문맥, 여기서는 ①과 같이 하나씩 판단(모델 차이만 보려고)",
    "점수: 논문은 Yes·No로 정규화한 P(Yes), 여기서는 평가 틀 공통 yes_probability"
    "(top_logprobs의 Yes 확률, No로 나누지 않음)",
    *_COMMON_DEVIATIONS,
)

# ⑤ Jev — ①②와 같은 트리·예산·노드 글·문구, 판단 모델과 공유 문맥만 다르게.
# "[Jev로 해소]"는 ①②의 논문과 다른 점 가운데 Jev로 없어진 것이다.
_MULTIPLE_CHOICE = "다지선다 재정렬 단계 없음(Jev 기능)"
JEV_RESOLVED = (
    "[Jev로 해소] 판단 모델: 논문과 같은 Jev(jev-1.13.0, TypeSafe 공식 API). 실제로 답한 모델은 "
    "결과 JSON params.usage.response_models",
    "[Jev로 해소] 후보 판단: 논문처럼 공유 문맥 — 한 단계의 후보를 state 하나에 함께 넣고 후보마다 "
    "noul 질문. 단 state가 Jev 토큰 24k(o200k 16,997)를 넘으면 번갈아 배정으로 나눠 보내고, "
    "나뉜 요청끼리는 문맥을 공유하지 않는다(나눈 단계 수는 params.usage.stages_split)",
    "[Jev로 해소] 점수: Jev noul = P(yes) 그대로(평가 틀의 yes_probability·Qwen yes/no softmax를 "
    "쓰지 않음). 응답이 소수 둘째 자리 단위라 동점이 생기고, 동점은 문서 순서(L1·L2)·"
    "parent_id(MaxP)",
)
JEV_DEVIATIONS = (
    *JEV_RESOLVED,
    *(
        f"{d} — Jev로 할 수 있으나 ①②와 같은 단계로 비교하려고 넣지 않음"
        if d == _MULTIPLE_CHOICE
        else d
        for d in _COMMON_DEVIATIONS
    ),
)

PART = "보통약관"
_P2 = "제2편 자동차보험에서 보상하는 내용"
_P2C1 = f"{_P2} > 제1장 배상책임"
_P2C2 = f"{_P2} > 제2장 배상책임 이외의 보장종목"
_P3 = "제3편 보험금 또는 손해배상의 청구"
_P4 = "제4편 일반사항"
ARTICLE_GROUPS: tuple[tuple[int, int, str], ...] = (  # (첫 조, 끝 조, 경로) — 원문 목차 p.45~47
    (1, 2, "제1편 용어의 정의 및 자동차보험의 구성"),
    (3, 5, f"{_P2C1} > 제1절 대인배상Ⅰ"),
    (6, 8, f"{_P2C1} > 제2절 대인배상Ⅱ와 대물배상"),
    (9, 11, f"{_P2C1} > 제3절 배상책임에서 공통으로 적용할 사항"),
    (12, 16, f"{_P2C2} > 제1절 자기신체사고"),
    (17, 20, f"{_P2C2} > 제2절 무보험자동차에 의한 상해"),
    (21, 24, f"{_P2C2} > 제3절 자기차량손해"),
    (25, 28, f"{_P3} > 제1장 피보험자의 보험금 청구"),
    (29, 32, f"{_P3} > 제2장 손해배상청구권자의 직접청구"),
    (33, 37, f"{_P3} > 제3장 보험금의 분담 등"),
    (38, 43, f"{_P4} > 제1장 보험계약의 성립"),
    (44, 46, f"{_P4} > 제2장 보험계약자 등의 의무"),
    (47, 54, f"{_P4} > 제3장 보험계약의 변동 및 보험료의 환급"),
    (55, 62, f"{_P4} > 제4장 그 밖의 사항"),
)  # fmt: skip
SCHEDULE_GROUPS: tuple[tuple[str, str], ...] = (  # 원문 목차 p.47
    ("별표1", "<별표 1> 대인배상, 무보험자동차에 의한 상해 지급 기준"),
    ("별표2", "<별표 2> 대물배상 지급 기준"),
    ("별표3", "<별표 3> 과실상계 등"),
    ("별표4", "<별표 4> 동승자 유형별 감액 비율표"),
    ("별표5", "<별표 5> 자기신체사고 지급 기준"),
)
# (첫 번호, 끝 번호, 묶음) — 원문 특약 목차 p.48~51
RIDER_GROUPS: tuple[tuple[int, int, str], ...] = (
    (1, 25, "Ⅰ. 운전가능자에 대한 제한"),
    (26, 38, "Ⅱ. 자기신체사고(자동차상해)의 보상 확대"),
    (39, 59, "Ⅲ. 자기차량손해의 보상 확대"),
    (60, 61, "Ⅳ. 사고처리 시 소요되는 비용"),
    (62, 63, "Ⅴ. 긴급출동 서비스"),
    (64, 67, "Ⅵ. 보험료 납입"),
    (68, 73, "Ⅶ. 친환경 상품"),
    (74, 101, "Ⅷ. 기타"),
)
LAW_GROUP = "부록 > 인용 법령(자동차손해배상 보장법 시행령 별표·인용 법조문·용어 찾아보기)"
_LAW_NAME = re.compile(r"\[\s*([^\[\]\n]{2,30}?(?:법|령|규칙))\s*\]")


@dataclass(frozen=True)
class GroupNode:
    group_id: str  # 경로(문서 순서로 정렬된 묶음)
    text: str  # 모델에 보여 줄 글
    parent_ids: tuple[str, ...]  # 문서 순서


@dataclass(frozen=True)
class ParentNode:
    parent_id: str
    group_id: str
    text: str


@dataclass(frozen=True)
class PolicyTree:
    groups: tuple[GroupNode, ...]
    parents: dict[str, ParentNode]
    version: str = TREE_VERSION


def _group_path(meta: dict) -> tuple[int, str] | None:
    """(문서 순서, 경로). 각주는 None(연결 조항을 따라간다)."""
    kind = meta["clause_type"]
    if kind == "general_article":
        number = int(meta["article_number"])
        for order, (first, last, path) in enumerate(ARTICLE_GROUPS):
            if first <= number <= last:
                return order, f"{PART} > {path}"
        raise ValueError(f"목차에 없는 조: {number}")
    if kind == "payment_standard":
        for order, (schedule, title) in enumerate(SCHEDULE_GROUPS, len(ARTICLE_GROUPS)):
            if meta.get("schedule") == schedule:
                return order, f"{PART} > {title}"
        raise ValueError(f"목차에 없는 별표: {meta.get('schedule')}")
    if kind == "rider":
        number = int(str(meta["rider_number"]).split("-")[0])
        start = len(ARTICLE_GROUPS) + len(SCHEDULE_GROUPS)
        for order, (first, last, title) in enumerate(RIDER_GROUPS, start):
            if first <= number <= last:
                return order, f"특별약관 > {title}"
        raise ValueError(f"목차에 없는 특약: {meta['rider_number']}")
    if kind.startswith("cited_law"):
        return len(ARTICLE_GROUPS) + len(SCHEDULE_GROUPS) + len(RIDER_GROUPS), LAW_GROUP
    if kind == "side_note":
        return None
    raise ValueError(f"알 수 없는 조항 종류: {kind}")


def build_tree(parents: PolicyParents, *, encoder: TokenEncoder | None = None) -> PolicyTree:
    """약관 parent 전부를 묶음 아래에 둔다.

    각주는 처음 연결한 조항(페이지 순)의 묶음과 제목을 따른다.
    """
    meta = {pid: parents.chunks(pid)[0]["metadata"] for pid in parents.parent_ids()}
    page = {pid: parents.chunks(pid)[0]["page_start"] for pid in meta}
    doc_order = sorted(meta, key=lambda pid: (page[pid], pid))
    owners: dict[str, list[str]] = {}
    for pid in doc_order:
        for note in meta[pid].get("side_note_parent_ids") or []:
            owners.setdefault(note, []).append(pid)
    placed: dict[str, tuple[int, str]] = {}
    labels: dict[str, str] = {}
    for pid in doc_order:
        found = _group_path(meta[pid])
        if found is not None:
            placed[pid] = found
            labels[pid] = parents.title(pid)
    for pid in doc_order:
        if pid in placed:
            continue
        owner = next((o for o in owners.get(pid, []) if o in placed), None)
        if owner is None:
            raise ValueError(f"각주를 붙일 조항이 없습니다: {pid}")
        placed[pid] = placed[owner]
        labels[pid] = f"{parents.title(owner)} 각주"
    members: dict[tuple[int, str], list[str]] = {}
    for pid in doc_order:
        members.setdefault(placed[pid], []).append(pid)
    groups = []
    for (_, path), pids in sorted(members.items()):
        text = _group_text(path, pids, meta, labels, parents, encoder)
        groups.append(GroupNode(path, text, tuple(pids)))
    nodes = {}
    for pid in doc_order:
        title = f"{placed[pid][1]} > {labels[pid]}"
        body = "\n".join(c["text"] for c in parents.chunks(pid))
        text = passage_text(title, body, encoder=encoder, max_tokens=NODE_BODY_TOKENS)
        nodes[pid] = ParentNode(pid, placed[pid][1], text)
    return PolicyTree(tuple(groups), nodes)


def _group_text(path, pids, meta, labels, parents, encoder) -> str:
    """묶음 글: 경로 + 하위 조항 제목(각주 제외). 인용 법령 페이지는 본문의 [법령명]을 모은다."""
    items: list[str] = []
    for pid in pids:
        kind = meta[pid]["clause_type"]
        if kind == "side_note":
            continue
        if kind == "cited_law_appendix":
            text = "\n".join(c["text"] for c in parents.chunks(pid))
            items += _LAW_NAME.findall(text)
            if "찾아보기" in text:
                items.append("찾아보기(용어 색인)")
        else:
            items.append(labels[pid])
    return passage_text(path, ", ".join(dict.fromkeys(items)), encoder=encoder)


# ---- 탐색 ----


class Scorer(Protocol):
    """판단 모델. (질문, 글 목록) → (점수 목록, 입력 토큰 수, 출력 토큰 수). 토큰은 모르면 None.

    글은 하나씩 따로 판단한다. score는 동기·async 둘 다 된다.
    """

    model_name: str

    def score(self, question: str, texts: Sequence[str]) -> object: ...


def _top(indices_scores: Sequence[float], k: int) -> list[int]:
    """점수 내림차순 상위 k개 위치. 동점은 앞(문서 순서) 먼저."""
    order = sorted(range(len(indices_scores)), key=lambda i: (-indices_scores[i], i))
    return order[:k]


class DecisionRagRetriever:
    """끼우는 함수. 정답은 받지 않는다. 단계마다 meter.stage로 남은 parent를 기록한다(채점용)."""

    def __init__(
        self,
        tree: PolicyTree,
        parents: PolicyParents,
        scorer: Scorer,
        *,
        group_budget: int = GROUP_BUDGET,
        parent_budget: int = PARENT_BUDGET,
    ) -> None:
        self.tree = tree
        self.parents = parents
        self.scorer = scorer
        self.group_budget = group_budget
        self.parent_budget = parent_budget
        self.params: dict = {}

    async def _score(self, question: Question, texts: Sequence[str], meter: Meter) -> list[float]:
        if not texts:
            return []
        meter.add(self.scorer.model_name, calls=len(texts))  # 부르기 전에 센다(상한)
        result = self.scorer.score(question.text, texts)
        if inspect.isawaitable(result):
            result = await result
        scores, input_tokens, output_tokens = result  # type: ignore[misc]
        if len(scores) != len(texts):
            raise RuntimeError(f"점수 수 {len(scores)} ≠ 글 수 {len(texts)}")
        if input_tokens is not None or output_tokens is not None:
            meter.add(
                self.scorer.model_name,
                calls=0,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        return scores

    async def __call__(self, question: Question, meter: Meter) -> list[str]:
        groups = self.tree.groups
        group_scores = await self._score(question, [g.text for g in groups], meter)
        kept_groups = [groups[i] for i in _top(group_scores, self.group_budget)]
        candidates = [pid for g in kept_groups for pid in g.parent_ids]
        meter.stage("L1", candidates)
        parent_scores = await self._score(
            question, [self.tree.parents[pid].text for pid in candidates], meter
        )
        kept = [candidates[i] for i in _top(parent_scores, self.parent_budget)]
        meter.stage("L2", kept)
        passages = [p for pid in kept for p in self.parents.passages(pid)]
        chunk_scores = await self._score(question, [p.text for p in passages], meter)
        ranked = max_p((p.parent_id, s) for p, s in zip(passages, chunk_scores, strict=True))
        return [pid for pid, _ in ranked[:TOP_K]]


def describe(
    tree: PolicyTree, retriever: DecisionRagRetriever, deviations: Sequence[str] = DEVIATIONS
) -> dict:
    """결과 JSON params에 남길 설정."""
    sizes = [len(g.parent_ids) for g in tree.groups]
    return {
        "name": NAME,
        "paper": PAPER,
        "corpus": CORPUS,
        "tree_version": tree.version,
        "tree": {"groups": len(tree.groups), "parents": len(tree.parents), "group_sizes": sizes},
        "budgets": {
            "L1_groups": retriever.group_budget,
            "L2_parents": retriever.parent_budget,
            "L3": "남은 parent의 모든 청크 → MaxP → 상위 10",
        },
        "node_body_tokens": NODE_BODY_TOKENS,
        "budgets_fixed_before_eval": True,
        "stages": {"L1": "정답 parent가 든 묶음이 남았나", "L2": "정답 parent가 남았나"},
        "deviations": list(deviations),
    }


def build(search):
    """run.py --retriever evals.rerank_compare.decisionrag:build. Qwen3-Reranker-0.6B를 읽는다."""
    from evals.rerank_compare.qwen_reranker import QwenReranker

    started = time.perf_counter()
    scorer = QwenReranker()
    load_seconds = time.perf_counter() - started
    tree = build_tree(search.parents)
    retriever = DecisionRagRetriever(tree, search.parents, scorer)
    retriever.params = describe(tree, retriever) | {
        "scorer": scorer.describe(),
        "model_load_seconds": load_seconds,
    }
    return retriever


def build_gpt(search):
    """run.py --retriever evals.rerank_compare.decisionrag:build_gpt. ②: 점수기만 gpt-4o-mini."""
    from evals.rerank_compare.gpt_judge import GptJudge

    scorer = GptJudge()
    tree = build_tree(search.parents)
    retriever = DecisionRagRetriever(tree, search.parents, scorer)
    retriever.params = describe(tree, retriever, GPT_DEVIATIONS) | {
        "name": f"{NAME} — ② gpt-4o-mini",
        "scorer": scorer.describe(),
    }
    retriever.after_run = lambda: {"usage": scorer.usage()}  # run.py가 실행 뒤 params에 더한다
    return retriever


def build_jev(search):
    """run.py --retriever evals.rerank_compare.decisionrag:build_jev. ⑤: 점수기만 Jev(공유 문맥)."""
    from evals.rerank_compare.jev_judge import JEV_MODEL, JevJudge

    scorer = JevJudge()
    tree = build_tree(search.parents)
    retriever = DecisionRagRetriever(tree, search.parents, scorer)
    retriever.params = describe(tree, retriever, JEV_DEVIATIONS) | {
        "name": f"{NAME} — ⑤ Jev(공유 문맥)",
        "scorer": scorer.describe(),
        "deviations_resolved_by_jev": list(JEV_RESOLVED),
    }

    retriever.after_run = lambda: jev_after_run(scorer.usage(), JEV_MODEL)
    return retriever


def jev_after_run(usage: dict, requested: str) -> dict:
    """⑤ 실행 뒤 params에 더할 것: 사용량, 요청과 다른 모델이 답했으면 모델 버전 차이."""
    extra: dict = {"usage": usage}
    answered = set(usage["response_models"])
    if answered and answered != {requested}:
        extra["deviations"] = [
            *JEV_DEVIATIONS,
            f"모델 버전: 논문 {requested} → 실제 응답 {', '.join(sorted(answered))}",
        ]
    return extra
