"""D: 면책·감액 조항 확인. 금액은 계산하지 않는다. LLM을 부르지 않는다(#204).

판정은 담당자 입력(case_facts의 무면허·음주·고의, FE 체크박스)으로만 한다(flag_judge).
- True(해당) → fail(면책 적용 → not_payable)
- False(아님) → pass("담당자 확인 — 해당 없음")
- 미입력(None) → unknown("면책 확인 필요 — 사실 미입력"). 해당 없음으로 가정하지 않는다.
  산정은 막지 않고(금액 계산, 상태 needs_contract_review) 산정 사유에 표시한다.
근거는 rules의 조항 ID(제23조)로 직접 가져온 청크다(벡터 검색 없음, C의 validate_hits로 검증).
결과와 상관없이 그 조항 ID를 근거로 붙인다. 조항을 가져오지 못했으면 담당자 입력이 있을 때는 그
입력으로 판정하고("약관 조항 미연결 — 담당자 입력 기준"), 없으면 "면책 확인 필요 — 조항 미확인".
예전에는 LLM 판정기가 있었으나, 받는 사실이 체크박스 3개뿐이라 입력이 없으면 근거 없이, 있으면
담당자보다 아는 게 없이 판정해 운영에서 15건이 모두 fail이었다(#204에서 뺐다).
체크박스 뜻은 "운전자(계약자·기명피보험자·30일 초과 임차인·동거 친족)의 무면허/음주"다.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.calculation.rules import DEFAULT_RULES, RuleSet
from app.schemas.reports import OwnDamageContractDoc, PreCheck
from app.schemas.retrieval import EvidenceHit
from app.tools._evidence import validate_hits

logger = logging.getLogger(__name__)

Judge = Callable[[str, list[EvidenceHit], dict[str, object]], PreCheck]
FetchFn = Callable[..., Awaitable[list[EvidenceHit]]]  # app.retrieval.common.fetch_by_parent

_FLAGS = ("무면허", "음주", "고의")
EXCLUSION_FACT_KEYS = _FLAGS  # case_facts에 허용되는 키(FE 면책 체크박스)
HANDLER_NONE = "담당자 확인 — 해당 없음"
NEEDS_FACTS = "면책 확인 필요 — 사실 미입력"
NEEDS_CLAUSE = "면책 확인 필요 — 조항 미확인"
NO_TERMS_NOTE = "약관 조항 미연결 — 담당자 입력 기준"


def _flag_key(topic: str) -> str | None:
    return next((k for k in _FLAGS if k in topic), None)


def _clause_ids(hits: list[EvidenceHit]) -> list[str]:
    return list(
        dict.fromkeys(h["chunk"]["parent_id"] for h in hits)
    )  # 한 조항이 여러 청크일 수 있음


def flag_judge(topic: str, hits: list[EvidenceHit], facts: dict[str, object]) -> PreCheck:
    """담당자 입력으로 한 항목 판정. 해당 = fail, 아님 = pass, 미입력 = unknown(가정하지 않음).

    hits(직접 조회한 조항)는 결과와 상관없이 근거 조항 ID로 붙인다.
    """
    key = _flag_key(topic)
    base: PreCheck = {
        "key": f"exclusion:{key or topic}",
        "label": topic,
        "result": "unknown",
        "decided_by": "code",
        "detail": "",
        "clause_ids": _clause_ids(hits),
    }
    if key is None:
        return {**base, "detail": "자동 판정 대상 아님 — 담당자 확인"}
    flag = facts.get(key)
    if flag is None:
        return {**base, "detail": NEEDS_FACTS}
    return {
        **base,
        "result": "fail" if flag else "pass",
        "decided_by": "handler",
        "detail": f"'{key}' 해당 — 면책 조항 적용" if flag else HANDLER_NONE,
    }


def _handler_check(topic: str, facts: dict[str, object]) -> PreCheck:
    """조항 없이 담당자 입력으로 판정. 면책 해당(fail)이면 "약관 조항 미연결"을 덧붙인다."""
    check = flag_judge(topic, [], facts)
    if check["result"] == "fail":
        check = {**check, "detail": f"{check['detail']} ({NO_TERMS_NOTE})"}
    return check


def review_exclusions_from_facts(
    facts: dict[str, object], *, rules: RuleSet = DEFAULT_RULES
) -> list[PreCheck]:
    """약관 검색이 연결되기 전: 검색 없이 담당자 입력(case_facts의 무면허·음주·고의)으로 판정.
    값이 없는 항목은 unknown("면책 확인 필요 — 사실 미입력")이다(해당 없음으로 가정하지 않음)."""
    return [_handler_check(topic, facts) for topic in rules.exclusion_topics]


async def fetch_exclusion_clauses(
    topic: str,
    *,
    edition: str | None,
    fetch: FetchFn,
    rules: RuleSet = DEFAULT_RULES,
) -> list[EvidenceHit]:
    """판정기에 넘길 근거: rules의 조항 ID로 직접 가져온 청크만.

    직접 조회는 C 도구를 거치지 않으므로 C의 validate_hits를 여기서 한 번 거친다. 검증에 실패하면
    경고 로그를 남기고 근거 없음으로 돌려준다(보고서를 막지 않음).
    """
    parents = rules.exclusion_clause_parents.get(topic, ())
    if not parents:
        return []
    hits = await fetch("policy_terms", list(parents), terms_edition=edition)
    request = {
        "corpus": "policy_terms",
        "text": topic,
        "accident_date": "",
        "filters": {}
        if edition is None
        else {"edition": edition},  # 없으면 기본 판 규칙으로 조회됨
        "top_k": max(len(hits), 1),
    }
    try:
        return await validate_hits(request, hits)
    except (ValueError, RuntimeError) as exc:  # 원문과 어긋남, 검증 저장소(EvidenceStore) 미등록
        logger.warning("면책 조항 검증 실패 — 조항 미확인으로 처리: %s (%s)", topic, exc)
        return []


def _check_without_clause(topic: str, facts: dict[str, object]) -> PreCheck:
    """조항을 찾지 못했을 때: 담당자 입력이 있으면 그대로 따르고, 없으면 표시만 한다."""
    key = _flag_key(topic)
    if key is not None and facts.get(key) is not None:
        return _handler_check(topic, facts)
    return {
        "key": f"exclusion:{key or topic}",
        "label": topic,
        "result": "unknown",
        "decided_by": "code",
        "detail": NEEDS_CLAUSE,
        "clause_ids": [],
    }


async def review_exclusions(
    contract: OwnDamageContractDoc | None,
    facts: dict[str, object],
    *,
    fetch: FetchFn,
    judge: Judge = flag_judge,
    rules: RuleSet = DEFAULT_RULES,
) -> list[PreCheck]:
    """주제마다 조항 ID로 직접 가져온 청크를 근거로 담당자 입력으로 판정한다(judge 기본 flag_judge).
    약관 적용판은 계약 값으로 필터한다(사고 일자로 대체하지 않음). 계약이 없으면(기본 가정) 판은
    공통 검색 설정이 정한다. 미입력(unknown) 항목은 막지 않고 표시만 한다.
    주제끼리는 서로 기대지 않으므로 동시에 부른다(asyncio.gather, 판정기는 스레드에서). 결과는
    rules.exclusion_topics 순서이고, 예상 밖 오류는 그대로 올린다(return_exceptions 안 씀)."""
    edition = contract.get("terms_edition") if contract else None

    async def review_topic(topic: str) -> PreCheck:
        hits = await fetch_exclusion_clauses(topic, edition=edition, fetch=fetch, rules=rules)
        if not hits:
            return _check_without_clause(topic, facts)
        check = await asyncio.to_thread(judge, topic, hits, facts)
        key = _flag_key(topic)
        if check["result"] == "unknown" and key is not None and facts.get(key) is None:
            check = {**check, "detail": NEEDS_FACTS}
        return check

    return list(await asyncio.gather(*(review_topic(t) for t in rules.exclusion_topics)))
