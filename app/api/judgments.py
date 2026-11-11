"""B: 확정 카드 판단 접수·질문 응답·질의응답.

- 판단은 요청 안에서 끝까지 실행한다(Vercel 함수 시간 안).
  진행 변화는 Supabase Realtime으로 FE에 간다.
- 실행·질문·응답·검증 로그는 judgment_runs 계열 테이블(app/workflow/runs.py), 그래프 체크포인트는
  langgraph 스키마(LangGraph AsyncPostgresSaver)에 저장한다. thread_id = judgment_runs.id.
- 응답은 TaskView(task_id = judgment_runs.id)다.
  새로고침·재접속 복원은 판단 조회(get_judgment — 최신 실행·판정·질문·답)가 맡는다.
  Realtime은 변경 알림만 준다.
- Idempotency-Key: 같은 키·같은 요청이면 저장한 응답을, 같은 키로 다른 요청이면 422를 돌려준다.
- 같은 입력(카드·인정기준·규칙 버전)은 다시 판단하지 않고 기존 실행을 돌려준다.
- 판정이 나면(세 상태 모두) D `reports.submit_judgment`로 보고서를 만든다. 같은 판단 버전의 보고서가
  있으면 다시 만들지 않는다. 보고서를 못 만들어도 판단은 완료이고 응답의 `report_error`로 알린다.
  같은 입력으로 판단 접수를 다시 보내면(새 키) 보고서 만들기만 다시 시도한다.
- 질의응답(ask_question)은 최신 판정과 그 판단에 쓴 카드(체크포인트)를 읽기만 한다.
  가정 실행(simulate)은 같은 그래프를 메모리 체크포인터로 돌려 아무것도 저장하지 않는다
  (app/agents/qa.py).
"""

import contextlib
import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from copy import deepcopy
from dataclasses import dataclass, field

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation

from app import db
from app.agents import qa as qa_agent
from app.api import reports
from app.api.errors import ApiException, forbidden, not_found, unprocessable, version_conflict
from app.schemas.common import ApiError
from app.schemas.facts import FactCard, FactField
from app.schemas.judgment import (
    AnswerView,
    JudgmentRunView,
    QaAnswer,
    QaHypothesis,
    QuestionAnswer,
    QuestionView,
)
from app.schemas.retrieval import EvidenceHit
from app.workflow import runs
from app.workflow.candidates import confirmed_values
from app.workflow.card_fields import to_card_terms
from app.workflow.graph import (
    CRITERIA_VERSION,
    RULE_VERSION,
    Dependencies,
    build_graph,
    check_attempts,
    initial_state,
    run_outcome,
    thread_config,
)

logger = logging.getLogger(__name__)

START_SCOPE = "POST /cases/{case_id}/judgments"

Connector = Callable[[], AbstractAsyncContextManager[AsyncConnection]]


@dataclass
class JudgmentServices:
    """바꿔 끼우는 의존성. 테스트는 가짜 검색·검토기와 테스트 DB 연결을 넣는다."""

    deps: Dependencies = field(default_factory=Dependencies)
    connect: Connector = db.connect
    criteria_version: str = CRITERIA_VERSION
    rule_version: str = RULE_VERSION
    qa: qa_agent.QaDependencies = field(default_factory=qa_agent.QaDependencies)


services = JudgmentServices()


def configure(**kw: object) -> JudgmentServices:
    global services
    services = JudgmentServices(**{**services.__dict__, **kw})  # type: ignore[arg-type]
    return services


def _uuid(value: object, what: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as exc:
        raise not_found(f"{what}을(를) 찾을 수 없습니다.") from exc


async def _require_assignee(conn: AsyncConnection, actor_id: str, case_id: str) -> None:
    assignee = await runs.case_assignee(conn, case_id)
    if assignee is None:
        raise not_found("사건을 찾을 수 없습니다.")
    if assignee != actor_id:
        raise forbidden()


def _task_view(run: runs.Row, *, status: str | None = None, stage: str | None = None) -> dict:
    return {
        "task_id": str(run["id"]), "case_id": str(run["case_id"]),
        "stage": stage or run["stage"], "status": status or run["status"],
        "input_version": run["fact_card_version"],
    }


def _judgment_view(
    run: runs.Row, *, status: str | None = None, stage: str | None = None,
    report_version: int | None = None, report_error: ApiError | None = None,
) -> dict:
    """판단 응답: TaskView + 이 판단으로 만든 보고서 버전(없으면 null)과 보고서 생성 오류."""
    return {**_task_view(run, status=status, stage=stage),
            "report_version": report_version, "report_error": report_error}


REPORT_FAILED: ApiError = {
    "code": "report_failed",
    "message": "판단은 끝났지만 보고서를 만들지 못했습니다. 판단을 다시 요청하면 다시 시도합니다.",
    "retryable": True,
}


async def _submit_report(
    conn: AsyncConnection, actor_id: str, card: FactCard, result: dict
) -> tuple[int | None, ApiError | None]:
    """판정 결과로 보고서를 만든다(D submit_judgment). (보고서 버전, 오류).

    같은 판단 버전의 보고서가 이미 있으면 다시 만들지 않는다(submit_judgment는 부를 때마다 새 버전).
    """
    versions = result["versions"]
    existing = await runs.report_version_for(
        conn, versions["case_id"], versions["judgment_version"]
    )
    if existing is not None:
        return existing, None
    try:
        report = await reports.submit_judgment(actor_id, card, result)  # type: ignore[arg-type]
    except ApiException as exc:
        logger.warning("report for case %s failed: %s", versions["case_id"], exc.error["code"])
        return None, exc.error
    except Exception:
        logger.exception("report for case %s failed", versions["case_id"])
        return None, REPORT_FAILED
    return report["versions"]["report_version"], None


def _request_hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _run_error(exc: BaseException) -> ApiError:
    """실행 실패를 FE에 보일 오류로. 내부 메시지는 서버 로그에만 남긴다."""
    if isinstance(exc, NotImplementedError):  # 검색 색인·반박 검토 모델 미연결
        return {
            "code": "dependency_not_ready",
            "message": "판단에 필요한 검색 색인·검토 모델 연결이 아직 준비되지 않았습니다.",
            "retryable": False,
        }
    return {
        "code": "judgment_failed",
        "message": "판단 실행 중 오류가 났습니다. 잠시 후 다시 요청하세요.",
        "retryable": True,
    }


async def _execute(
    conn: AsyncConnection, run: runs.Row, graph_input: object, actor_id: str
) -> dict:
    """그래프를 처음 실행하거나(초기 상태) 재개(Command)하고 결과를 저장한다.

    판정이 나면 보고서를 만든다.
    """
    run_id, case_id = str(run["id"]), str(run["case_id"])
    graph = build_graph(services.deps, AsyncPostgresSaver(conn))
    config = thread_config(run_id)
    try:
        values = await graph.ainvoke(graph_input, config)
        outcome = run_outcome(values)
    except Exception as exc:
        logger.exception("judgment run %s failed", run_id)
        stage = run["stage"]
        with contextlib.suppress(Exception):
            snapshot = await graph.aget_state(config)
            stage = snapshot.next[0] if snapshot.next else stage
        await runs.save_failed(conn, run_id, stage, _run_error(exc))
        return _judgment_view(run, status="failed", stage=stage)
    if outcome["status"] == "waiting_for_confirmation":
        await runs.save_waiting(
            conn, run_id, case_id, outcome["questions"], outcome["rebuttal_retry_count"]
        )
        return _judgment_view(run, status=outcome["status"], stage=outcome["stage"])
    result = outcome["result"]
    assert result is not None
    await runs.save_completed(
        conn, run_id, case_id, result, check_attempts(result["checks"]),
        outcome["rebuttal_retry_count"],
    )
    version, error = await _submit_report(conn, actor_id, values["card"], result)
    return _judgment_view(run, status=outcome["status"], stage=outcome["stage"],
                          report_version=version, report_error=error)


async def start_judgment(
    actor_id: str, card: FactCard, idempotency_key: str
) -> dict[str, object]:
    """권한·카드 확정 확인 후 고정 그래프 실행. 카드 버전 일치는 라우트가 먼저 검사한다."""
    case_id = _uuid(card["case_id"], "사건")
    if card["status"] != "confirmed":
        raise unprocessable("card_not_confirmed", "담당자가 확정한 사실 카드만 판단할 수 있습니다.")
    request_hash = _request_hash({"case_id": case_id, "card_version": card["version"]})
    async with services.connect() as conn:
        await _require_assignee(conn, actor_id, case_id)
        stored = await runs.claim_key(
            conn, actor_id, START_SCOPE, idempotency_key, request_hash, case_id
        )
        if stored is not None:
            return _replay(stored, request_hash)
        try:
            response = await _start(conn, actor_id, case_id, card)
        except BaseException:
            with contextlib.suppress(Exception):
                await runs.release_key(conn, actor_id, START_SCOPE, idempotency_key)
            raise
        await runs.finish_key(
            conn, actor_id, START_SCOPE, idempotency_key, 202, response, str(response["task_id"])
        )
        return response


def _replay(stored: runs.Row, request_hash: str) -> dict[str, object]:
    if stored["request_hash"] != request_hash:
        raise unprocessable(
            "idempotency_key_reused",
            "같은 Idempotency-Key로 다른 요청을 보냈습니다. 새 키를 쓰세요.",
        )
    if stored["response_body"] is None:
        raise ApiException(
            409, "request_in_progress",
            "같은 요청을 처리하고 있습니다. 잠시 후 다시 조회하세요.", True,
        )
    return stored["response_body"]


async def _start(conn: AsyncConnection, actor_id: str, case_id: str, card: FactCard) -> dict:
    try:
        run, created = await runs.open_run(
            conn, case_id=case_id, card_version=card["version"],
            criteria_version=services.criteria_version, rule_version=services.rule_version,
            actor_id=actor_id,
        )
    except runs.RunBusy as exc:
        raise ApiException(
            409, "judgment_in_progress",
            "이 사건의 판단이 실행 중입니다. 끝난 뒤 다시 요청하세요.", True,
        ) from exc
    except runs.StaleCard as exc:
        raise version_conflict(
            "확인 대기 중인 판단보다 오래된 카드 버전입니다. 최신 카드를 확인하세요."
        ) from exc
    except UniqueViolation as exc:  # 사건 행 잠금을 지나서도 겹친 경우(다른 경로의 쓰기)
        raise ApiException(
            409, "judgment_conflict", "같은 사건의 판단 요청이 겹쳤습니다. 다시 시도하세요.", True
        ) from exc
    card = {**card, "case_id": case_id}  # type: ignore[typeddict-item]
    if not created:
        if run["status"] != "completed" or run["result"] is None:
            return _judgment_view(run)
        # 같은 입력의 완료된 판단: 보고서가 없으면(이전 생성 실패) 다시 만든다
        version, error = await _submit_report(conn, actor_id, card, run["result"])
        return _judgment_view(run, report_version=version, report_error=error)
    state = initial_state(
        card, services.criteria_version, services.rule_version, run["judgment_version"],
    )
    return await _execute(conn, run, state, actor_id)


async def answer_question(
    actor_id: str, case_id: str, answer: QuestionAnswer
) -> dict[str, object]:
    """답변 저장. 모두 '모름'이면 같은 실행을 재개한다.

    값 답변이 있으면 카드 수정·재확정으로 넘긴다.

    값 답변은 곧바로 판단에 쓰지 않는다. A의 카드 수정/확정으로 새 카드 버전이 생기면
    POST /judgments가 새 실행을 만들고 이 실행을 대체한다(runs.open_run).
    """
    case_id = _uuid(case_id, "사건")
    question_id = _uuid(answer["question_id"], "질문")
    async with services.connect() as conn:
        await _require_assignee(conn, actor_id, case_id)
        question = await runs.load_question(conn, question_id)
        if question is None or str(question["case_id"]) != case_id:
            raise not_found("질문을 찾을 수 없습니다.")
        if question["run_status"] != "waiting_for_confirmation" or question["superseded_at"]:
            raise unprocessable(
                "question_closed", "이미 끝났거나 새 카드 버전으로 대체된 판단의 질문입니다."
            )
        if answer["expected_card_version"] != question["fact_card_version"]:
            raise version_conflict("질문을 받은 카드 버전과 다릅니다. 최신 판단을 확인하세요.")
        _check_answer(question, answer)
        if not await runs.insert_answer(conn, question, answer, actor_id):
            raise ApiException(409, "already_answered", "이미 답한 질문입니다.")
        return await _after_answer(conn, str(question["run_id"]), actor_id)


def _check_answer(question: runs.Row, answer: QuestionAnswer) -> None:
    if answer["unknown"]:
        if answer["value"] is not None:
            raise unprocessable("invalid_answer", "'모름' 응답에는 값을 넣지 않습니다.")
        if not question["allow_unknown"]:
            raise unprocessable("invalid_answer", "이 질문은 '모름'으로 답할 수 없습니다.")
        return
    if answer["value"] is None:
        raise unprocessable("invalid_answer", "선택지 중 하나를 고르거나 '모름'으로 답하세요.")
    if question["options"] and answer["value"] not in question["options"]:
        raise unprocessable("invalid_answer", "질문의 선택지에 없는 값입니다.")


async def _after_answer(conn: AsyncConnection, run_id: str, actor_id: str) -> dict[str, object]:
    rows = await runs.run_questions(conn, run_id)
    run = await runs.get_run(conn, run_id)
    assert run is not None
    pending = [str(r["id"]) for r in rows if not r["answered"]]
    view: dict[str, object] = {
        **_judgment_view(run), "pending_question_ids": pending, "next_action": None,
        "field_values": {},
    }
    if pending:
        return {**view, "next_action": "answer_remaining"}
    values = {r["field_key"]: r["value"] for r in rows if not r["unknown"]}
    if values:
        return {**view, "next_action": "update_fact_card", "field_values": values}
    claimed = await runs.claim_resume(conn, run_id)
    if claimed is None:  # 다른 요청이 먼저 재개했다
        return view
    payload = [
        {"question_id": str(r["id"]), "expected_card_version": r["expected_card_version"],
         "value": None, "unknown": True}
        for r in rows
    ]
    return {**view, **await _execute(conn, claimed, Command(resume=payload), actor_id)}


async def get_judgment(actor_id: str, case_id: str) -> JudgmentRunView | None:
    """사건의 최신 판단 실행과 구분 질문·답. 판단을 시작하지 않은 사건이면 None.

    판정이 난 실행이면 그 판단으로 만든 보고서의 최신 버전을 싣는다.
    확인 대기 실행은 result가 비어 있으니 질문은 questions에서 읽는다.
    담당이 아니면 403, 사건이 없으면 404다.
    """
    case_id = _uuid(case_id, "사건")
    async with services.connect() as conn:
        await _require_assignee(conn, actor_id, case_id)
        run = await runs.latest_run(conn, case_id)
        if run is None:
            return None
        questions = await runs.question_views(conn, str(run["id"]))
        report_version = None
        if run["status"] == "completed":
            report_version = await runs.report_version_for(conn, case_id, run["judgment_version"])
    return {
        "task_id": str(run["id"]), "case_id": str(run["case_id"]),
        "judgment_version": run["judgment_version"], "fact_card_version": run["fact_card_version"],
        "criteria_version": run["criteria_version"], "rule_version": run["rule_version"],
        "status": run["status"], "stage": run["stage"],
        "rebuttal_retry_count": run["rebuttal_retry_count"],
        "result": run["result"], "error": run["error"], "report_version": report_version,
        "questions": [_question_view(row) for row in questions],
        "updated_at": run["updated_at"].isoformat(),
    }


def _question_view(row: runs.Row) -> QuestionView:
    answer: AnswerView | None = None
    if row["answered"]:
        answer = {
            "value": row["value"], "unknown": row["unknown"],
            "expected_card_version": row["expected_card_version"],
            "resulting_card_version": row["resulting_card_version"],
            "answered_at": row["answered_at"].isoformat(),
        }
    return {
        "question_id": str(row["id"]), "field_key": row["field_key"], "text": row["text"],
        "options": list(row["options"]), "allow_unknown": row["allow_unknown"], "answer": answer,
    }


async def ask_question(actor_id: str, case_id: str, text: str) -> QaAnswer:
    """근거 질의응답·what-if(1차 확장). 최신 판단이 판정을 낸 사건만 받는다.

    DB에서는 최신 판정과 그 판단에 쓴 카드(체크포인트)를 읽기만 하고
    모델을 부르기 전에 연결을 놓는다.
    공식 판단·보고서·확인 완료 상태는 바꾸지 않는다. 담당이 아니면 403, 사건이 없으면 404,
    판정이 난 판단이 없으면 409 `judgment_required`, 모델 키가 없으면 503 `dependency_not_ready`,
    LLM 호출 시간 상한(질문당 합계·호출 1회)을 넘으면 504 `qa_timeout`,
    모델 호출·응답 오류는 502 `qa_failed`(둘 다 다시 질문 가능).
    """
    case_id = _uuid(case_id, "사건")
    async with services.connect() as conn:
        await _require_assignee(conn, actor_id, case_id)
        run = await runs.latest_run(conn, case_id)
        if run is None or run["status"] != "completed" or run["result"] is None:
            raise ApiException(
                409, "judgment_required",
                "판정이 난 판단이 있어야 질의응답을 할 수 있습니다. 판단을 먼저 끝내세요.", False,
            )
        card = await _run_card(conn, str(run["id"]))
    context = qa_agent.QaContext(
        case_id=case_id, judgment_version=run["judgment_version"],
        accident_date=str(card["accident_date"]) if card else "", result=run["result"],
        confirmed_facts=confirmed_values(card) if card else {},
        simulate=_simulator(card) if card else None,
    )
    try:
        return await qa_agent.answer_question(text, context, services.qa)
    except ApiException:
        raise
    except Exception as exc:
        raise _qa_error(exc, case_id) from exc


def _qa_error(exc: Exception, case_id: str) -> ApiException:
    """질의응답 실패를 FE에 보일 오류로. 내부 메시지는 서버 로그에만 남긴다."""
    if isinstance(exc, qa_agent.QaNotConfigured):
        return ApiException(
            503, "dependency_not_ready", "질의응답 모델 연결이 아직 준비되지 않았습니다.", False
        )
    if isinstance(exc, qa_agent.QaTimedOut):
        logger.warning("qa for case %s timed out: %s", case_id, exc)
        return ApiException(
            504, "qa_timeout",
            "답을 만드는 데 시간이 너무 오래 걸렸습니다. 질문을 짧게 나눠 다시 물어보세요.", True,
        )
    logger.error("qa for case %s failed", case_id, exc_info=exc)
    return ApiException(
        502, "qa_failed", "질의응답 중 오류가 났습니다. 잠시 후 다시 질문하세요.", True
    )


async def _run_card(conn: AsyncConnection, run_id: str) -> FactCard | None:
    """공식 판단에 쓴 카드(그래프 체크포인트 상태). 못 읽으면 None — 가정 실행만 끈다."""
    graph = build_graph(services.deps, AsyncPostgresSaver(conn))
    try:
        snapshot = await graph.aget_state(thread_config(run_id))
    except Exception:
        logger.exception("checkpoint read for run %s failed", run_id)
        return None
    card = (snapshot.values or {}).get("card")
    return card if isinstance(card, dict) else None  # type: ignore[return-value]


def _simulator(card: FactCard) -> qa_agent.Simulator:
    async def simulate(changes: dict[str, str]) -> tuple[QaHypothesis, list[EvidenceHit]]:
        return await _simulate(card, changes)

    return simulate


def _assumed(base: FactField | None, key: str, value: str) -> FactField:
    """가정 값을 담당자 확인 값처럼 넣는다(그래프는 human_confirmed 칸만 본다). 저장하지 않는다."""
    field_: FactField = base or {
        "value": None, "model_value": None, "source": "manual", "model_confidence": None,
        "verification_status": "unverified", "evidence_frame_ids": [], "source_document_ids": [],
    }
    typed: str | bool = value == "true" if key == "night" else value
    return {**field_, "value": typed, "verification_status": "human_confirmed"}


async def _simulate(
    card: FactCard, changes: dict[str, str]
) -> tuple[QaHypothesis, list[EvidenceHit]]:
    """가정 실행: 카드 사본의 칸을 바꿔 공식과 같은 그래프를 메모리 체크포인터로 돌린다.

    판단 버전·실행 행·체크포인트·보고서를 만들지 않는다.
    질문으로 멈추면 그 칸을 open_fields로 돌려준다.
    """
    hypo = deepcopy(card)
    for key, value in changes.items():
        # 가정 값은 도표 선택 칸·값이다. v1 맞바꿈 카드면 카드 쪽 칸·값으로 되돌려 넣는다
        card_key, card_value = to_card_terms(key, value, card)
        hypo["fields"][card_key] = _assumed(hypo["fields"].get(card_key), card_key, card_value)
    view: QaHypothesis = {
        "changes": dict(changes), "status": "failed", "judgment_status": None, "chart_id": None,
        "fault_A": None, "fault_B": None, "reasons": [], "open_fields": [], "error": None,
    }
    graph = build_graph(services.deps)  # 메모리 체크포인터: 아무것도 저장하지 않는다
    state = initial_state(hypo, services.criteria_version, services.rule_version, None)
    try:
        outcome = run_outcome(
            await graph.ainvoke(state, thread_config(f"qa-simulate-{uuid.uuid4()}"))
        )
    except Exception as exc:
        if not isinstance(exc, NotImplementedError):
            logger.exception("qa simulate for case %s failed", card.get("case_id"))
        return {**view, "error": _run_error(exc)}, []
    if outcome["status"] == "waiting_for_confirmation":
        return {
            **view, "status": "waiting_for_confirmation",
            "open_fields": [q["field_key"] for q in outcome["questions"]],
            "reasons": ["가정한 사실로도 확인할 칸이 남아 판정 전에 멈췄다"],
        }, []
    result = outcome["result"]
    assert result is not None
    return {
        **view, "status": "completed", "judgment_status": result["status"],
        "chart_id": result["chart_id"], "fault_A": result["fault_A"], "fault_B": result["fault_B"],
        "reasons": list(result["reasons"]),
    }, list(result["evidence"])
