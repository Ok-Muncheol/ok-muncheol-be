"""D: 면책·감액 조항 확인. 금액은 계산하지 않는다.

근거는 rules의 조항 ID로 직접 가져온 청크뿐이다(벡터 검색 없음, C의 validate_hits로 검증).
판정기는 '이 사고에 적용되는가'만 답한다. 조항 ID가 색인에 없으면 담당자 입력을 따르고,
입력도 없으면 "면책 확인 필요"로 표시만 한다(막지 않음).
- FlagJudge: LLM 없이 담당자 입력 플래그(무면허·음주·고의)로 판정 — 테스트·오프라인용.
  약관 검색이 연결되기 전에는 review_exclusions_from_facts가 검색 없이 이 판정기로 판정한다.
  FE 체크박스(무면허·음주·고의·해당 없음) → case_facts: 미입력(None) = 해당 없음으로 가정(assumed),
  False = 담당자 확인 — 해당 없음(pass), True = 면책 적용(fail → not_payable).
- LLMJudge: LLM으로 판정. 근거 조항 ID가 검색 결과에 없으면 버리고, 근거 없이 면책 판정을 내지
  않는다. 호출 1회 상한·재시도는 app/llm.py 정책(45초, SDK 재시도 끔, 일시 오류만 1회)이다.
  동기 호출이라 review_exclusions가 판정기를 스레드에서 부른다(이벤트 루프를 막지 않게).
담당자 입력이 LLM보다 우선한다(apply_handler_priority). 체크박스 뜻은 "운전자(계약자·기명피보험자·
30일 초과 임차인·동거 친족)의 무면허/음주"다. 입력이 없을 때만 LLM 결과를 따른다.
"""

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from app import llm as llm_policy
from app.calculation.rules import DEFAULT_RULES, RuleSet
from app.retrieval.embedding import ROOT, RetrievalNotConfigured
from app.schemas.reports import OwnDamageContractDoc, PreCheck
from app.schemas.retrieval import EvidenceHit
from app.tools._evidence import validate_hits

logger = logging.getLogger(__name__)

Judge = Callable[[str, list[EvidenceHit], dict[str, object]], PreCheck]
FetchFn = Callable[..., Awaitable[list[EvidenceHit]]]  # app.retrieval.common.fetch_by_parent

_FLAGS = ("무면허", "음주", "고의")
EXCLUSION_FACT_KEYS = _FLAGS  # case_facts에 허용되는 키(FE 면책 체크박스)
FACT_MISSING = "면책 사실 미입력 — 해당 없음으로 가정"
HANDLER_NONE = "담당자 확인 — 해당 없음"
NEEDS_FACTS = "면책 확인 필요 — 사실 미입력"
NEEDS_CLAUSE = "면책 확인 필요 — 조항 미확인"


def _flag_key(topic: str) -> str | None:
    return next((k for k in _FLAGS if k in topic), None)


def _clause_ids(hits: list[EvidenceHit]) -> list[str]:
    return list(
        dict.fromkeys(h["chunk"]["parent_id"] for h in hits)
    )  # 한 조항이 여러 청크일 수 있음


def flag_judge(topic: str, hits: list[EvidenceHit], facts: dict[str, object]) -> PreCheck:
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
        return {**base, "result": "assumed", "detail": FACT_MISSING}
    return {
        **base,
        "result": "fail" if flag else "pass",
        "detail": f"'{key}' 해당 — 면책 조항 적용" if flag else HANDLER_NONE,
    }


PROMPT = """너는 자동차보험 보상 담당자를 돕는 검토 보조다. 금액은 계산하지 않는다.
아래 약관 조항이 이 사고에 적용되는지만 판단하라.

[확인할 사유] {topic}
[사고 사실] {facts}
[약관 조항]
{clauses}

JSON 한 개로만 답하라:
{{"result": "fail" | "pass" | "unknown", "clause_ids": [근거 조항 ID], "detail": "한두 문장"}}
"fail"은 면책·감액 조항이 적용된다는 뜻이다. 사실이 부족하면 반드시 "unknown"."""


def llm_judge(llm: Callable[[str], str]) -> Judge:
    def judge(topic: str, hits: list[EvidenceHit], facts: dict[str, object]) -> PreCheck:
        valid = set(_clause_ids(hits))
        prompt = PROMPT.format(
            topic=topic,
            facts=json.dumps(facts, ensure_ascii=False),
            clauses="\n".join(f"- [{h['chunk']['parent_id']}] {h['chunk']['text']}" for h in hits),
        )
        base: PreCheck = {
            "key": f"exclusion:{topic}",
            "label": topic,
            "result": "unknown",
            "decided_by": "llm",
            "detail": "",
            "clause_ids": [],
        }
        try:
            raw = llm(prompt)
            data = json.loads(raw[raw.index("{") : raw.rindex("}") + 1])
        except (ValueError, json.JSONDecodeError):
            return {**base, "detail": "LLM 응답 해석 실패 — 담당자 확인"}
        ids = [i for i in data.get("clause_ids", []) if i in valid]  # 환각 인용 차단
        result = (
            data.get("result") if data.get("result") in ("pass", "fail", "unknown") else "unknown"
        )
        if result == "fail" and not ids:
            result = "unknown"  # 근거 조항 없는 면책 판정 금지
        return {
            **base,
            "result": result,
            "clause_ids": ids,
            "detail": str(data.get("detail", ""))[:300],
        }

    return judge


def openai_judge_llm(
    *,
    dotenv_path: Path | None = None,
    client: object | None = None,
    retry: bool = True,
) -> Callable[[str], str]:
    """llm_judge에 넘길 호출 함수. 모델은 .env의 OPENAI_MODEL. 키는 저장하지 않는다.

    같은 프롬프트는 다시 부르지 않는다(재산정마다 비용이 들고, 답이 흔들리면 같은 입력에 새 버전이
    생기기 때문). 호출 실패는 키가 섞일 수 있는 원문 없이 ValueError로 올린다 → llm_judge가
    "담당자 확인"(unknown)으로 내려 보고서를 막지 않는다.
    호출 1회 상한·재시도는 app/llm.py 정책이다(호출 1회 45초, 타임아웃·네트워크·5xx만 1회 더).
    retry=False면 다시 시도하지 않는다(호출 수를 세는 시험 스크립트용).
    """
    hint = 'py -3.12 -m pip install -e ".[embedding]"로 설치하세요'
    try:
        from dotenv import load_dotenv
    except ImportError:
        raise RetrievalNotConfigured(f"python-dotenv가 없습니다. {hint}") from None
    load_dotenv(dotenv_path or ROOT / ".env", override=False)
    model = os.environ.get("OPENAI_MODEL")
    if not os.environ.get("OPENAI_API_KEY") or not model:
        raise RetrievalNotConfigured(
            ".env에 OPENAI_API_KEY, OPENAI_MODEL이 필요합니다. 키 값은 출력하지 않습니다."
        )
    if client is None:
        try:
            client = llm_policy.openai_sync_client()
        except ImportError:
            raise RetrievalNotConfigured(f"openai 패키지가 없습니다. {hint}") from None
    cache: dict[str, str] = {}

    def call(prompt: str) -> str:
        if prompt in cache:
            return cache[prompt]

        def attempt() -> str:
            response = client.chat.completions.create(  # type: ignore[attr-defined]
                model=model, messages=[{"role": "user", "content": prompt}]
            )
            return response.choices[0].message.content or ""

        failure = None
        answer = ""
        try:
            answer = llm_policy.with_retry_sync(attempt, name="exclusion") if retry else attempt()
        except Exception as exc:  # 원문에 키 일부가 섞일 수 있어 오류 종류만 남긴다
            failure = type(exc).__name__
        if failure:  # except 밖에서 올려 원본 예외가 __context__에 남지 않게 한다
            raise ValueError(f"LLM 호출 실패: {failure}")
        cache[prompt] = answer
        return answer

    return call


NO_TERMS_NOTE = "약관 조항 미연결 — 담당자 입력 기준"


def _handler_check(topic: str, facts: dict[str, object]) -> PreCheck:
    """담당자 입력으로 한 항목 판정. 면책 해당(fail)이면 "약관 조항 미연결" 근거를 붙인다."""
    check = flag_judge(topic, [], facts)
    detail = (
        f"{check['detail']} ({NO_TERMS_NOTE})" if check["result"] == "fail" else check["detail"]
    )
    return {**check, "decided_by": "handler", "detail": detail}


def review_exclusions_from_facts(
    facts: dict[str, object], *, rules: RuleSet = DEFAULT_RULES
) -> list[PreCheck]:
    """약관 검색이 연결되기 전: 검색 없이 담당자 입력(case_facts의 무면허·음주·고의)으로 판정.
    값이 없는 항목은 해당 없음으로 가정(assumed)한다."""
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


def apply_handler_priority(topic: str, check: PreCheck, facts: dict[str, object]) -> PreCheck:
    """LLM 판정에 담당자 입력을 우선 적용한다. 입력이 없으면 LLM 결과 그대로.

    True → LLM 결과와 상관없이 면책(fail), LLM이 인용한 조항은 근거로 표시.
    False → 통과. LLM이 fail이면 통과하되 경고를 남긴다.
    """
    key = _flag_key(topic)
    flag = facts.get(key) if key is not None else None
    if check["decided_by"] != "llm" or flag is None:
        return check
    ids = ", ".join(check["clause_ids"])
    if flag:
        cited = f"근거 조항 {ids}" if ids else "인용 조항 없음"
        detail = f"담당자 입력 — 면책 적용 (LLM 판정 {check['result']}, {cited})"
        return {**check, "result": "fail", "decided_by": "handler", "detail": detail}
    passed: PreCheck = {**check, "result": "pass", "decided_by": "handler", "detail": HANDLER_NONE}
    if check["result"] == "fail":
        warning = f"LLM은 면책 가능성 제시(조항 {ids}) — 담당자 확인"
        passed.update({"detail": warning, "warning": warning})
    return passed


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
    """주제마다 조항 ID로 직접 가져온 청크로 판정. 약관 적용판은 계약 값으로 필터한다(사고 일자로
    대체하지 않음). 계약이 없으면(기본 가정) 판은 공통 검색 설정이 정한다. 사실이 없어 unknown이
    나온 항목은 막지 않고 표시만 한다.
    판정기는 스레드에서 부른다(LLM 판정기는 동기 호출이라 이벤트 루프를 막는다)."""
    out: list[PreCheck] = []
    edition = contract.get("terms_edition") if contract else None
    for topic in rules.exclusion_topics:
        hits = await fetch_exclusion_clauses(topic, edition=edition, fetch=fetch, rules=rules)
        if not hits:
            out.append(_check_without_clause(topic, facts))
            continue
        judged = await asyncio.to_thread(judge, topic, hits, facts)
        check = apply_handler_priority(topic, judged, facts)
        key = _flag_key(topic)
        if check["result"] == "unknown" and key is not None and facts.get(key) is None:
            check = {**check, "detail": NEEDS_FACTS}
        out.append(check)
    return out
