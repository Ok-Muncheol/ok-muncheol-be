"""B: 고정 판단 그래프(LangGraph). 확정 카드 → 범위 → 후보 → 질문/재개 → 선택 → 검증 3겹 → 판정.

- 공식 판단은 이 그래프만 만든다. LLM은 반박 검토(②)에서만 쓰고 비율은 코드로 계산한다.
- 구분 질문은 `interrupt`로 멈춘다. 담당자가 값을 답하면 A의 카드 수정/확정 경로로 새 카드 버전이
  생기고 새 실행이 시작된다(같은 실행을 잇지 않는다). "모른다"고 답하면 같은 실행을 재개해
  보류로 내린다.
- 재개 값은 API 계층이 먼저 검증한다. 재개 뒤 노드가 예외를 내면 LangGraph가 같은 재개 값으로
  다시 실패하므로, 노드 안에서는 담당자 입력으로 예외를 내지 않는다.
- 반박이 wrong_chart면 차순위 도표로 MAX_REBUTTAL_RETRIES 만큼만 재실행한다.
  missing_fact·contradiction과 상한을 넘긴 wrong_chart는 재실행 없이 needs_confirmation으로 내린다.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, TypedDict, get_args

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from app.schemas.chart_scope import (
    EXCLUDED_MODIFIERS,
    FIELD_OPTIONS,
    IN_SCOPE_ACCIDENT_TYPES,
    IN_SCOPE_LOCATION_TYPES,
    MANUAL_ONLY_MODIFIERS,
    MODIFIER_REQUIRED_CONFIRMED_FIELDS,
    PENDING_DECISION_LOCATION_TYPES,
    REASON_FIELD_NOT_CONFIRMED,
    REASON_NO_REVIEWED_CHART,
    REASON_OUT_OF_SCOPE,
    REASON_PENDING_DECISION,
    REQUIRED_CONFIRMED_FIELDS,
    Modifier,
    unconfirmed_fields,
)
from app.schemas.facts import FactCard
from app.schemas.judgment import (
    ChartAlternative,
    ConfirmationQuestion,
    JudgmentResult,
    ModifierDecision,
    QuestionAnswer,
    ValidationCheck,
)
from app.validation.evidence import check_evidence
from app.validation.numerical import calculation_lines, check_numerical
from app.validation.rebuttal import Reviewer, review_rebuttal
from app.workflow.candidates import ChartCandidate, confirmed_values, retrieve_from_corpus
from app.workflow.state import AppliedModifier, ChartSelection, JudgmentState, Outcome

STAGES = (
    "require_confirmed_card",
    "check_chart_scope",
    "retrieve_chart_candidates",
    "ask_or_resume",
    "select_chart_and_modifiers",
    "check_evidence",
    "review_rebuttal",
    "calculate_and_validate_fault",
    "build_judgment_result",
)
MAX_REBUTTAL_RETRIES = 1
# 실행 입력 버전(judgment_runs). 같은 카드·인정기준·규칙 버전이면 다시 판단하지 않는다.
# 인정기준은 data/corpora/fault_standards/manifest.json의 적용판이다.
# 규칙 버전은 이 그래프·검증 규칙을 바꿀 때 올린다
CRITERIA_VERSION = "fault_standards:2023.6"
RULE_VERSION = "judgment-rules-2026.11.10"
AUTO_MODIFIER_CODES: tuple[str, ...] = get_args(Modifier)
QUESTION_NAMESPACE = uuid.UUID("5d1f6c4e-1b2a-4a3c-9e8d-700000000001")

FIELD_LABELS = {
    "accident_type": "사고 당사자 유형", "location_type": "교차로 종류",
    "road_priority": "도로 관계(동일폭·대로소로·일시정지)",
    "movement_A": "A 차량의 진행 방향", "movement_B": "B 차량의 진행 방향",
    "signal_A": "A 차량의 신호", "signal_B": "B 차량의 신호",
    "entry_order": "교차로 진입 순서(A 기준)", "night": "야간 여부",
}

Retriever = Callable[[FactCard], Awaitable[list[ChartCandidate]]]


class UnconfirmedCardError(ValueError):
    """확정되지 않은 카드가 그래프에 들어왔다. API가 먼저 막아야 하는 프로그래밍 오류다."""


@dataclass(frozen=True)
class Dependencies:
    """바꿔 끼울 외부 의존성. 기본은 C 검색툴·OpenAI 반박 검토다."""

    retrieve: Retriever = retrieve_from_corpus
    reviewer: Reviewer | None = None


def scope_gate(fields: dict[str, dict[str, object]]) -> tuple[str | None, list[str]]:
    """(보류 사유, 질문할 칸). 범위 밖이면 보류, 범위 안이면 미확인 칸을 질문으로 넘긴다.

    docs/contracts/mvp-chart-scope.md 범위. 값은 human_confirmed 인 것만 본다.
    """
    def confirmed(key: str) -> object:
        f = fields.get(key, {})
        return f.get("value") if f.get("verification_status") == "human_confirmed" else None

    accident_type, location_type = confirmed("accident_type"), confirmed("location_type")
    if accident_type is None or location_type is None:
        return None, unconfirmed_fields(fields, "other")
    if location_type in PENDING_DECISION_LOCATION_TYPES:
        return REASON_PENDING_DECISION, []
    if accident_type not in IN_SCOPE_ACCIDENT_TYPES or location_type not in IN_SCOPE_LOCATION_TYPES:
        return REASON_OUT_OF_SCOPE, []
    return None, unconfirmed_fields(fields, str(location_type))


def initial_state(
    card: FactCard, criteria_version: str, rule_version: str, judgment_version: int | None = None
) -> JudgmentState:
    return {
        "case_id": card["case_id"], "card": card, "criteria_version": criteria_version,
        "rule_version": rule_version, "judgment_version": judgment_version,
        "rebuttal_retry_count": 0, "evidence": [], "questions": [], "checks": [],
        "candidates": [], "unknown_fields": [], "excluded_chart_ids": [],
        "selection": None, "outcome": None,
    }


def thread_config(thread_id: str) -> RunnableConfig:
    """체크포인트 스레드 = judgment_runs.id."""
    return {"configurable": {"thread_id": thread_id}}


class RunOutcome(TypedDict):
    """그래프 호출 한 번의 결과. 질문으로 멈췄거나(result 없음) 판정이 났다."""

    status: Literal["waiting_for_confirmation", "completed"]
    stage: str
    rebuttal_retry_count: int
    questions: list[ConfirmationQuestion]
    result: JudgmentResult | None


def run_outcome(values: dict) -> RunOutcome:
    """`ainvoke` 반환값 → RunOutcome. 질문으로 멈추면 LangGraph가 `__interrupt__`를 붙인다."""
    retries = int(values.get("rebuttal_retry_count", 0))
    interrupts = values.get("__interrupt__") or []
    if interrupts:
        return {
            "status": "waiting_for_confirmation", "stage": "ask_or_resume",
            "rebuttal_retry_count": retries, "questions": interrupts[0].value["questions"],
            "result": None,
        }
    result = values.get("result")
    if result is None:
        raise RuntimeError("판단 그래프가 질문도 결과도 없이 끝났다")
    return {
        "status": "completed", "stage": "build_judgment_result",
        "rebuttal_retry_count": retries, "questions": [], "result": result,
    }


def check_attempts(checks: list[ValidationCheck]) -> list[int]:
    """검증 결과마다 시도 번호(0 첫 도표, 1 차순위 재실행) — validation_checks.attempt.

    도표 오선택 반박이 성립하면 그다음 검증부터 다음 시도다. 재실행 상한이 1이라 1을 넘지 않는다.
    """
    attempts: list[int] = []
    attempt = 0
    for check in checks:
        attempts.append(attempt)
        wrong_chart = check.get("rebuttal_kind") == "wrong_chart"
        if check["layer"] == "rebuttal" and not check["passed"] and wrong_chart:
            attempt += 1
    return attempts


def question_id(thread_id: str, field_key: str) -> str:
    """같은 실행·같은 칸이면 재개 뒤에도 같은 ID(judgment_questions.id로 그대로 저장한다)."""
    return str(uuid.uuid5(QUESTION_NAMESPACE, f"{thread_id}:{field_key}"))


def build_question(thread_id: str, field_key: str) -> ConfirmationQuestion:
    return {
        "question_id": question_id(thread_id, field_key), "field_key": field_key,
        "text": f"{FIELD_LABELS.get(field_key, field_key)}을(를) 확인해 주세요.",
        "options": list(FIELD_OPTIONS.get(field_key, ())), "allow_unknown": True,
    }


def _eligible(state: JudgmentState) -> list[ChartCandidate]:
    excluded = set(state.get("excluded_chart_ids", []))
    return [
        c for c in state.get("candidates", [])
        if c["calculation_ready"] and c["chart_id"] not in excluded
    ]


def _required_fields(card: FactCard) -> tuple[str, ...]:
    location = confirmed_values(card).get("location_type", "other")
    return REQUIRED_CONFIRMED_FIELDS.get(location, REQUIRED_CONFIRMED_FIELDS["other"])  # type: ignore[arg-type]


def open_fields(state: JudgmentState) -> list[str]:
    """아직 묻지 않은 구분 칸: 범위 필수 칸 + 후보 도표가 가르는 칸. 확인됐거나 '모른다'면 뺀다."""
    card = state["card"]
    values = confirmed_values(card)
    unknown = set(state.get("unknown_fields", []))
    ordered: list[str] = list(_required_fields(card))
    for candidate in _eligible(state):
        ordered += list(candidate["attributes"])
        for variant in candidate["base_variants"]:
            ordered += list(variant["conditions"])
        for modifier in candidate["modifiers"]:
            if modifier["code"] in AUTO_MODIFIER_CODES and modifier["conditions"] is not None:
                ordered += list(modifier["conditions"])
    result: list[str] = []
    for key in ordered:
        if key in FIELD_OPTIONS and key not in values and key not in unknown and key not in result:
            result.append(key)
    return result


def _rank(state: JudgmentState) -> tuple[list[ChartCandidate], list[tuple[str, str]]]:
    """확인한 칸과 모순 없는 검수 후보를 일치 칸 수·검색 점수로 정렬한다.

    (순위, [(도표, 제외 이유)]).
    """
    card = state["card"]
    values = confirmed_values(card)
    unknown = set(state.get("unknown_fields", []))
    required = set(_required_fields(card))
    ranked: list[tuple[int, float, ChartCandidate]] = []
    skipped: list[tuple[str, str]] = []
    for candidate in _eligible(state):
        keys = set(candidate["attributes"])
        for variant in candidate["base_variants"]:
            keys |= set(variant["conditions"])
        if not required <= keys:
            skipped.append((candidate["chart_id"], f"구분 칸 {sorted(required - keys)} 미정의"))
            continue
        mismatch = [k for k, v in candidate["attributes"].items() if k in values and values[k] != v]
        if mismatch:
            skipped.append((candidate["chart_id"], f"카드와 불일치 {mismatch}"))
            continue
        unverifiable = [k for k in candidate["attributes"] if k not in values and k in unknown]
        if unverifiable:
            skipped.append((candidate["chart_id"], f"확인 불가 칸 {unverifiable}"))
            continue
        matched = sum(1 for k in candidate["attributes"] if k in values)
        ranked.append((matched, candidate["score"], candidate))
    ranked.sort(key=lambda item: (-item[0], -item[1]))
    ordered = [c for _, _, c in ranked]
    preferred = state.get("preferred_chart_id")
    if preferred and any(c["chart_id"] == preferred for c in ordered):
        ordered.sort(key=lambda c: c["chart_id"] != preferred)
    return ordered, skipped


def _pick_variant(candidate: ChartCandidate, values: dict[str, str]) -> tuple[object, list[str]]:
    """조건이 모두 확인된 변형을 고른다. 없으면 (None, 확인해야 할 칸)."""
    missing: list[str] = []
    for variant in candidate["base_variants"]:
        conditions = variant["conditions"]
        if all(values.get(k) == v for k, v in conditions.items()):
            return variant, []
        missing += [k for k in conditions if k not in values and k not in missing]
    return None, missing


def _apply_modifiers(
    candidate: ChartCandidate, values: dict[str, str]
) -> tuple[list[AppliedModifier], list[tuple[object, str]]]:
    applied: list[AppliedModifier] = []
    unresolved: list[tuple[object, str]] = []
    for modifier in candidate["modifiers"]:
        code = modifier["code"]
        if code in EXCLUDED_MODIFIERS or code in MANUAL_ONLY_MODIFIERS:
            unresolved.append((modifier, "자동 적용 제외(담당자 입력 전용)"))
            continue
        conditions, delta = modifier["conditions"], modifier["delta"]
        if code not in AUTO_MODIFIER_CODES or conditions is None or delta is None:
            unresolved.append((modifier, "조건·가감치를 코드로 판정할 수 없음"))
            continue
        needed = [k for k in MODIFIER_REQUIRED_CONFIRMED_FIELDS[code] if k not in values]  # type: ignore[index]
        needed += [k for k in conditions if k not in values and k not in needed]
        if needed:
            unresolved.append((modifier, f"확인되지 않은 칸: {', '.join(needed)}"))
            continue
        if all(values.get(k) == v for k, v in conditions.items()):
            applied.append({
                "modifier_id": modifier["modifier_id"], "party": modifier["party"],
                "name": modifier["name"], "code": code, "delta": delta,
            })
        else:
            unresolved.append((modifier, "적용 조건 불일치"))
    return applied, unresolved


def modifier_decisions(
    candidate: ChartCandidate, applied: list[AppliedModifier],
    unresolved: list[tuple[object, str]],
) -> list[ModifierDecision]:
    """보고서 5번. 도표 레코드 순서대로, 가감치가 있는 요소만 담는다.

    가감치가 없는(비적용) 요소는 이 도표의 대상이 아니다.
    """
    applied_ids = {a["modifier_id"] for a in applied}
    why_by_id = {m["modifier_id"]: why for m, why in unresolved}  # type: ignore[index]
    decisions: list[ModifierDecision] = []
    for modifier in candidate["modifiers"]:
        if modifier["delta"] is None:
            continue
        fields = [*MODIFIER_REQUIRED_CONFIRMED_FIELDS.get(modifier["code"], ()),  # type: ignore[call-overload]
                  *(modifier["conditions"] or {})]
        if modifier["modifier_id"] in applied_ids:
            decision, why = "적용", "적용 조건 충족"
        else:
            why = why_by_id.get(modifier["modifier_id"], "판정하지 않음")
            decision = "미적용" if why == "적용 조건 불일치" else "불명"
        decisions.append({
            "modifier_id": modifier["modifier_id"], "name": modifier["name"],
            "applies_to": modifier["party"], "delta": modifier["delta"],
            "decision": decision, "evidence_fields": list(dict.fromkeys(fields)),  # type: ignore[typeddict-item]
            "reason": why,
        })
    return decisions


def alternative_reasons(
    state: JudgmentState, best: ChartCandidate, others: list[ChartCandidate],
    skipped: list[tuple[str, str]], values: dict[str, str],
) -> list[ChartAlternative]:
    """보고서 3번 차순위 후보.

    반박으로 뺀 도표 → 차순위 → 카드와 맞지 않는 후보 → 검수 전 레코드 순이다.
    """
    out: list[ChartAlternative] = []
    rebutted = [ch for ch in state["checks"] if ch["layer"] == "rebuttal" and not ch["passed"]
                and ch.get("rebuttal_kind") == "wrong_chart"]
    for chart_id, check in zip(state.get("excluded_chart_ids", []), rebutted, strict=False):
        reason = "; ".join(check["reasons"]) or "도표 오선택"
        out.append({"chart_id": chart_id, "reason": f"반박 검토로 제외 — {reason}"})
    best_matched = sum(1 for k in best["attributes"] if k in values)
    for candidate in others:
        matched = sum(1 for k in candidate["attributes"] if k in values)
        if matched < best_matched:
            why = f"확인한 칸 {matched}개 일치(선택 도표 {best_matched}개)"
        elif matched == best_matched:
            why = "확인한 칸 일치 수가 같고 검색 순위가 낮음"
        else:
            why = "반박 검토가 고른 도표를 우선함"
        out.append({"chart_id": candidate["chart_id"], "reason": f"차순위 — {why}"})
    out += [{"chart_id": chart_id, "reason": f"제외 — {why}"} for chart_id, why in skipped]
    out += [
        {"chart_id": c["chart_id"], "reason": "제외 — 검수 전 도표 레코드"}
        for c in state.get("candidates", []) if not c["calculation_ready"]
    ]
    return out


def _outcome(status: str, reasons: list[str]) -> Outcome:
    return {"status": status, "reasons": reasons}  # type: ignore[typeddict-item]


def build_graph(
    deps: Dependencies | None = None, checkpointer: BaseCheckpointSaver | None = None
) -> CompiledStateGraph:
    """C 검색 도구·검증 3겹 연결. 체크포인터가 없으면 메모리(테스트·로컬)."""
    deps = deps or Dependencies()

    def require_confirmed_card(state: JudgmentState) -> dict:
        card = state["card"]
        if card["status"] != "confirmed":
            raise UnconfirmedCardError("확정된 사실 카드만 판단할 수 있다")
        mapping = card["party_mapping"]
        a, b = mapping["chart_party_A_id"], mapping["chart_party_B_id"]
        update: dict = {"stage": "require_confirmed_card"}
        if not a or not b or a == b:
            update["outcome"] = _outcome(
                "needs_confirmation",
                ["당사자 매핑 미확정: 도표 A/B에 대응하는 당사자를 확인해야 한다"],
            )
        return update

    def check_chart_scope(state: JudgmentState) -> dict:
        reason, _ = scope_gate(state["card"]["fields"])  # type: ignore[arg-type]
        update: dict = {"stage": "check_chart_scope"}
        if reason:
            update["outcome"] = _outcome("deferred", [reason])
        return update

    async def retrieve_chart_candidates(state: JudgmentState) -> dict:
        candidates = await deps.retrieve(state["card"])
        update: dict = {
            "stage": "retrieve_chart_candidates", "candidates": candidates,
            "evidence": [hit for c in candidates for hit in c["hits"]],
        }
        if not any(c["calculation_ready"] for c in candidates):
            reasons = [REASON_NO_REVIEWED_CHART]
            reasons += [f"{c['chart_id']}: {r}" for c in candidates for r in c.get("reasons", [])]
            update["outcome"] = _outcome("deferred", reasons)
        return update

    def ask_or_resume(state: JudgmentState, config: RunnableConfig) -> dict:
        fields = open_fields(state)
        if not fields:
            return {"stage": "ask_or_resume", "questions": []}
        thread_id = str(config.get("configurable", {}).get("thread_id") or state["case_id"])
        questions = [build_question(thread_id, key) for key in fields]
        answers: list[QuestionAnswer] = interrupt({"questions": questions})
        unknown_ids = {
            a["question_id"] for a in answers
            if isinstance(a, dict) and a.get("unknown") and a.get("value") is None
        }
        unknown = list(state.get("unknown_fields", []))
        unknown += [q["field_key"] for q in questions
                    if q["question_id"] in unknown_ids and q["field_key"] not in unknown]
        return {"stage": "ask_or_resume", "questions": questions, "unknown_fields": unknown}

    def select_chart_and_modifiers(state: JudgmentState) -> dict:
        card = state["card"]
        values = confirmed_values(card)
        unknown = [k for k in _required_fields(card) if k in state.get("unknown_fields", [])]
        update: dict = {"stage": "select_chart_and_modifiers", "selection": None}
        if unknown:
            update["outcome"] = _outcome(
                "deferred", [REASON_FIELD_NOT_CONFIRMED.format(field_key=k) for k in unknown]
            )
            return update
        ranked, skipped = _rank(state)
        if not ranked:
            excluded = [f"{chart_id}: {why}" for chart_id, why in skipped]
            update["outcome"] = _outcome("deferred", [REASON_NO_REVIEWED_CHART, *excluded])
            return update
        best = ranked[0]
        variant, missing = _pick_variant(best, values)
        if variant is None:
            update["outcome"] = _outcome(
                "deferred", [REASON_FIELD_NOT_CONFIRMED.format(field_key=k) for k in missing]
                or [f"{best['chart_id']}: 기본 과실 변형 조건 불일치"]
            )
            return update
        applied, unresolved = _apply_modifiers(best, values)
        matched = [k for k in best["attributes"] if k in values]
        selection: ChartSelection = {
            "chart_id": best["chart_id"], "parent_id": best["parent_id"],
            "edition": best["edition"], "variant": variant,  # type: ignore[typeddict-item]
            "applied_modifiers": applied, "unresolved_modifiers": unresolved,  # type: ignore[typeddict-item]
            "alternatives": [c["chart_id"] for c in ranked[1:]],
            "reasons": [
                f"{best['chart_id']} 선택: 확인한 칸 {len(matched)}개 일치 ({', '.join(matched)})",
            ],
            "modifier_decisions": modifier_decisions(best, applied, unresolved),
            "alternative_reasons": alternative_reasons(state, best, ranked[1:], skipped, values),
        }
        update["selection"] = selection
        update["selected_chart_id"] = best["chart_id"]
        return update

    def check_evidence_node(state: JudgmentState) -> dict:
        selection = state["selection"]
        assert selection is not None
        accident_date = state["card"]["accident_date"]
        check = check_evidence(selection, state.get("candidates", []), accident_date)
        update: dict = {"stage": "check_evidence", "checks": [*state["checks"], check]}
        if not check["passed"]:
            update["outcome"] = _outcome("deferred", [REASON_NO_REVIEWED_CHART, *check["reasons"]])
        return update

    async def review_rebuttal_node(state: JudgmentState) -> dict:
        selection = state["selection"]
        assert selection is not None
        check, preferred = await review_rebuttal(
            state["card"], selection, state.get("candidates", []), deps.reviewer
        )
        update: dict = {"stage": "review_rebuttal", "checks": [*state["checks"], check]}
        if check["passed"]:
            return update
        kind = check.get("rebuttal_kind")
        retries = state["rebuttal_retry_count"]
        if kind == "wrong_chart" and retries < MAX_REBUTTAL_RETRIES and preferred:
            update["rebuttal_retry_count"] = retries + 1
            excluded = state.get("excluded_chart_ids", [])
            update["excluded_chart_ids"] = [*excluded, selection["chart_id"]]
            update["preferred_chart_id"] = preferred
            return update
        label = {
            "wrong_chart": "두 번째 도표 오선택 반박: 담당자 확인 필요",
            "missing_fact": "도표를 가르는 사실이 카드에 없음",
            "contradiction": "카드 사실과 도표 조건이 모순",
        }.get(kind or "", "반박 성립")
        update["outcome"] = _outcome("needs_confirmation", [label, *check["reasons"]])
        return update

    def calculate_and_validate_fault(state: JudgmentState) -> dict:
        selection = state["selection"]
        assert selection is not None
        variant = selection["variant"]
        base = (variant["base_A"], variant["base_B"])
        modifiers = selection["applied_modifiers"]
        lines, fault = calculation_lines(base[0], base[1], modifiers)  # type: ignore[arg-type]
        provisional = _result(state, "draft_ready", selection["reasons"], fault, lines, [])
        check = check_numerical(provisional, base, modifiers)  # type: ignore[arg-type]
        update: dict = {
            "stage": "calculate_and_validate_fault", "checks": [*state["checks"], check],
            "fault": fault, "calculation_log": lines,
        }
        if not check["passed"]:
            update["outcome"] = _outcome("deferred", ["수치 검증 실패", *check["reasons"]])
        return update

    def build_judgment_result(state: JudgmentState) -> dict:
        outcome = state.get("outcome")
        status = outcome["status"] if outcome else "draft_ready"
        selection = state.get("selection")
        reasons = outcome["reasons"] if outcome else (selection["reasons"] if selection else [])
        fault = state.get("fault") if status == "draft_ready" else None
        log = state.get("calculation_log", []) if status == "draft_ready" else []
        questions = [build_question(state["case_id"], k) for k in open_fields(state)] \
            if status == "needs_confirmation" else []
        result = _result(state, status, reasons, fault, log, questions)
        return {"stage": "build_judgment_result", "result": result}

    def _result(
        state: JudgmentState, status: str, reasons: list[str], fault: tuple[int, int] | None,
        log: list[str], questions: list[ConfirmationQuestion],
    ) -> JudgmentResult:
        selection = state.get("selection")
        keep_chart = selection is not None and status != "deferred"
        chart_hits = [
            hit for c in state.get("candidates", [])
            if selection and c["chart_id"] == selection["chart_id"] for hit in c["hits"]
        ]
        return {
            "versions": {
                "case_id": state["case_id"], "fact_card_version": state["card"]["version"],
                "judgment_version": state.get("judgment_version"), "report_version": None,
            },
            "status": status,  # type: ignore[typeddict-item]
            "chart_id": selection["chart_id"] if keep_chart and selection else None,
            "edition": selection["edition"] if keep_chart and selection else None,
            "fault_A": fault[0] if fault else None, "fault_B": fault[1] if fault else None,
            "calculation_log": list(log), "evidence": chart_hits if keep_chart else [],
            "checks": list(state["checks"]), "questions": questions, "reasons": list(reasons),
            "base_fault_A": selection["variant"]["base_A"] if keep_chart and selection else None,
            "modifiers": list(selection["modifier_decisions"]) if keep_chart and selection else [],
            "alternatives": (
                list(selection["alternative_reasons"]) if keep_chart and selection else []
            ),
        }

    def _after(next_node: str):
        return lambda state: "build_judgment_result" if state.get("outcome") else next_node

    def _after_ask(state: JudgmentState) -> str:
        return "ask_or_resume" if open_fields(state) else "select_chart_and_modifiers"

    def _after_rebuttal(state: JudgmentState) -> str:
        """판정이 났으면 결과로, 반박이 성립했는데 판정이 없으면 차순위 재실행, 아니면 계산."""
        if state.get("outcome"):
            return "build_judgment_result"
        if not state["checks"][-1]["passed"]:
            return "select_chart_and_modifiers"
        return "calculate_and_validate_fault"

    graph = StateGraph(JudgmentState)
    graph.add_node("require_confirmed_card", require_confirmed_card)
    graph.add_node("check_chart_scope", check_chart_scope)
    graph.add_node("retrieve_chart_candidates", retrieve_chart_candidates)
    graph.add_node("ask_or_resume", ask_or_resume)
    graph.add_node("select_chart_and_modifiers", select_chart_and_modifiers)
    graph.add_node("check_evidence", check_evidence_node)
    graph.add_node("review_rebuttal", review_rebuttal_node)
    graph.add_node("calculate_and_validate_fault", calculate_and_validate_fault)
    graph.add_node("build_judgment_result", build_judgment_result)
    graph.add_edge(START, "require_confirmed_card")
    graph.add_conditional_edges("require_confirmed_card", _after("check_chart_scope"))
    graph.add_conditional_edges("check_chart_scope", _after("retrieve_chart_candidates"))
    graph.add_conditional_edges("retrieve_chart_candidates", _after("ask_or_resume"))
    graph.add_conditional_edges("ask_or_resume", _after_ask)
    graph.add_conditional_edges("select_chart_and_modifiers", _after("check_evidence"))
    graph.add_conditional_edges("check_evidence", _after("review_rebuttal"))
    graph.add_conditional_edges("review_rebuttal", _after_rebuttal)
    graph.add_conditional_edges("calculate_and_validate_fault", _after("build_judgment_result"))
    graph.add_edge("build_judgment_result", END)
    return graph.compile(checkpointer=checkpointer or InMemorySaver())
