"""B: 판단 실행 저장소.

대상 테이블: judgment_runs·judgment_questions·judgment_answers·validation_checks·
api_idempotency_keys. 제약은 `supabase/migrations/20261109163901_judgment_runs.sql`이 원천이다.
연결의 search_path 첫 자리는 LangGraph 체크포인트 스키마라(app/db.py) 판단 테이블에는
모두 `public.`을 붙인다. 쓰기는 BE만 한다.
"""

from typing import Any

from psycopg import AsyncConnection
from psycopg.errors import UndefinedTable
from psycopg.types.json import Jsonb

from app.schemas.common import ApiError
from app.schemas.judgment import ConfirmationQuestion, JudgmentResult, QuestionAnswer

RUN_COLUMNS = (
    "id, case_id, judgment_version, fact_card_version, criteria_version, rule_version, status,"
    " stage, rebuttal_retry_count, result, error, resumed_from_run_id, superseded_at, updated_at"
)
# Vercel 함수 최대 실행 시간보다 길게. 이보다 오래 running이면 함수가 중간에 끊긴 실행이다
STALE_RUNNING = "10 minutes"
STALE_ERROR: ApiError = {
    "code": "run_interrupted",
    "message": "판단 실행이 중간에 멈춰 실패로 정리했습니다. 다시 요청하세요.",
    "retryable": True,
}

Row = dict[str, Any]


class RunBusy(Exception):  # noqa: N818 — 서비스가 409로 바꾸는 상태 이름
    """같은 사건의 판단이 지금 실행 중이다."""


class StaleCard(Exception):  # noqa: N818
    """확인 대기 중인 판단보다 오래된 카드 버전으로 요청했다."""


async def _one(conn: AsyncConnection, sql: str, params: tuple = ()) -> Row | None:
    cur = await conn.execute(sql, params)
    return await cur.fetchone()  # type: ignore[return-value]


async def setup_checkpoints(conn: AsyncConnection) -> None:
    """LangGraph 체크포인트 테이블을 langgraph 스키마에 만든다(scripts/setup_checkpointer.py)."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    await AsyncPostgresSaver(conn).setup()


async def case_assignee(conn: AsyncConnection, case_id: str) -> str | None:
    row = await _one(conn, "select assignee_id from public.cases where id = %s", (case_id,))
    return str(row["assignee_id"]) if row else None


# ── 멱등성 키 ─────────────────────────────────────────────────────
async def claim_key(
    conn: AsyncConnection, actor_id: str, scope: str, key: str, request_hash: str, case_id: str
) -> Row | None:
    """새 키면 선점하고 None. 이미 있으면 그 행(request_hash·response_status·response_body).

    만료된 키는 지우고 새로 선점한다. 동시에 같은 키가 오면 한쪽만 선점한다(기본 키).
    """
    async with conn.transaction():
        await conn.execute(
            "delete from public.api_idempotency_keys"
            " where actor_id = %s and scope = %s and key = %s and expires_at < now()",
            (actor_id, scope, key),
        )
        claimed = await _one(
            conn,
            "insert into public.api_idempotency_keys (actor_id, scope, key, request_hash, case_id)"
            " values (%s, %s, %s, %s, %s) on conflict do nothing returning key",
            (actor_id, scope, key, request_hash, case_id),
        )
        if claimed:
            return None
        return await _one(
            conn,
            "select request_hash, response_status, response_body from public.api_idempotency_keys"
            " where actor_id = %s and scope = %s and key = %s",
            (actor_id, scope, key),
        )


async def finish_key(
    conn: AsyncConnection, actor_id: str, scope: str, key: str,
    status: int, body: dict[str, object], resource_id: str,
) -> None:
    await conn.execute(
        "update public.api_idempotency_keys"
        " set response_status = %s, response_body = %s, resource_id = %s"
        " where actor_id = %s and scope = %s and key = %s",
        (status, Jsonb(body), resource_id, actor_id, scope, key),
    )


async def release_key(conn: AsyncConnection, actor_id: str, scope: str, key: str) -> None:
    """응답을 저장하지 못하고 끝난 요청의 선점을 푼다. 같은 키로 다시 보낼 수 있다."""
    await conn.execute(
        "delete from public.api_idempotency_keys"
        " where actor_id = %s and scope = %s and key = %s and response_status is null",
        (actor_id, scope, key),
    )


# ── 판단 실행 ─────────────────────────────────────────────────────
async def get_run(conn: AsyncConnection, run_id: str) -> Row | None:
    return await _one(
        conn, f"select {RUN_COLUMNS} from public.judgment_runs where id = %s", (run_id,)
    )


async def latest_run(conn: AsyncConnection, case_id: str) -> Row | None:
    """사건의 최신 판단 실행 = 판단 버전이 가장 큰 실행. 판단 전이면 None.

    새 카드 버전의 판단이 확인 대기 실행을 대체하면 그 새 실행이 더 큰 버전이라
    대체된 실행은 오지 않는다.
    실패한 실행도 최신이면 그대로 돌려준다(다시 요청하면 새 버전이 생긴다).
    """
    return await _one(
        conn,
        f"select {RUN_COLUMNS} from public.judgment_runs where case_id = %s"
        " order by judgment_version desc limit 1",
        (case_id,),
    )


async def open_run(
    conn: AsyncConnection, *, case_id: str, card_version: int, criteria_version: str,
    rule_version: str, actor_id: str,
) -> tuple[Row, bool]:
    """(실행, 새로 만들었는지).

    같은 입력의 실패하지 않은 실행이 있으면 그것을 돌려준다(재판단 금지).

    - 확인 대기 중인 이전 실행은 대체(superseded_at)하고 새 실행을 resumed_from_run_id로 잇는다.
      그 실행의 값 응답에는 이번 카드 버전을 resulting_card_version으로 남긴다.
    - 실행 중(queued·running)인 판단이 있으면 RunBusy. 오래 멈춘 running은 failed로 정리한다.
    - 사건 행을 잠가 판단 버전 번호를 겹치지 않게 매긴다.
    """
    async with conn.transaction():
        await conn.execute("select id from public.cases where id = %s for update", (case_id,))
        await conn.execute(
            "update public.judgment_runs"
            " set status = 'failed', error = %s, completed_at = now()"
            " where case_id = %s and status in ('queued', 'running') and superseded_at is null"
            f" and updated_at < now() - interval '{STALE_RUNNING}'",
            (Jsonb(STALE_ERROR), case_id),
        )
        same = await _one(
            conn,
            f"select {RUN_COLUMNS} from public.judgment_runs where case_id = %s"
            " and fact_card_version = %s and criteria_version = %s and rule_version = %s"
            " and status <> 'failed'",
            (case_id, card_version, criteria_version, rule_version),
        )
        if same is not None:
            return same, False
        active = await _one(
            conn,
            f"select {RUN_COLUMNS} from public.judgment_runs where case_id = %s"
            " and status in ('queued', 'running', 'waiting_for_confirmation')"
            " and superseded_at is null",
            (case_id,),
        )
        resumed_from = None
        if active is not None:
            if active["status"] != "waiting_for_confirmation":
                raise RunBusy
            if active["fact_card_version"] > card_version:
                raise StaleCard
            await conn.execute(
                "update public.judgment_runs set superseded_at = now() where id = %s",
                (active["id"],),
            )
            await conn.execute(
                "update public.judgment_answers a set resulting_card_version = %s"
                " from public.judgment_questions q"
                " where a.question_id = q.id and q.run_id = %s and not a.unknown"
                " and a.resulting_card_version is null and a.expected_card_version < %s",
                (card_version, active["id"], card_version),
            )
            resumed_from = active["id"]
        version = await _one(
            conn,
            "select coalesce(max(judgment_version), 0) + 1 as next"
            " from public.judgment_runs where case_id = %s",
            (case_id,),
        )
        run = await _one(
            conn,
            "insert into public.judgment_runs (case_id, judgment_version, fact_card_version,"
            " criteria_version, rule_version, status, stage, resumed_from_run_id, requested_by)"
            " values (%s, %s, %s, %s, %s, 'running', 'require_confirmed_card', %s, %s)"
            f" returning {RUN_COLUMNS}",
            (case_id, version["next"], card_version, criteria_version, rule_version,  # type: ignore[index]
             resumed_from, actor_id),
        )
        return run, True  # type: ignore[return-value]


async def save_waiting(
    conn: AsyncConnection, run_id: str, case_id: str,
    questions: list[ConfirmationQuestion], retry_count: int,
) -> None:
    """구분 질문으로 멈춘 실행. 질문 ID는 그래프가 정한 값을 그대로 쓴다(재개해도 같다)."""
    async with conn.transaction():
        await conn.execute(
            "update public.judgment_runs set status = 'waiting_for_confirmation',"
            " stage = 'ask_or_resume', rebuttal_retry_count = %s where id = %s",
            (retry_count, run_id),
        )
        for q in questions:
            await conn.execute(
                "insert into public.judgment_questions"
                " (id, run_id, case_id, field_key, text, options, allow_unknown)"
                " values (%s, %s, %s, %s, %s, %s, %s) on conflict do nothing",
                (q["question_id"], run_id, case_id, q["field_key"], q["text"], q["options"],
                 q["allow_unknown"]),
            )


async def save_completed(
    conn: AsyncConnection, run_id: str, case_id: str, result: JudgmentResult,
    attempts: list[int], retry_count: int,
) -> None:
    """판정이 난 실행. result가 원천이고 판정 칼럼은 DB가 생성한다.

    검증 로그는 시도 번호와 함께 남긴다.
    """
    async with conn.transaction():
        await conn.execute(
            "update public.judgment_runs set status = 'completed',"
            " stage = 'build_judgment_result', result = %s, rebuttal_retry_count = %s,"
            " error = null, completed_at = now() where id = %s",
            (Jsonb(result), retry_count, run_id),
        )
        for check, attempt in zip(result["checks"], attempts, strict=True):
            await conn.execute(
                "insert into public.validation_checks"
                " (run_id, case_id, attempt, layer, passed, reasons)"
                " values (%s, %s, %s, %s, %s, %s) on conflict do nothing",
                (run_id, case_id, attempt, check["layer"], check["passed"], check["reasons"]),
            )


async def save_failed(conn: AsyncConnection, run_id: str, stage: str, error: ApiError) -> None:
    await conn.execute(
        "update public.judgment_runs set status = 'failed', stage = %s, error = %s,"
        " completed_at = now() where id = %s",
        (stage, Jsonb(error), run_id),
    )


# ── 구분 질문·응답 ────────────────────────────────────────────────
async def load_question(conn: AsyncConnection, question_id: str) -> Row | None:
    return await _one(
        conn,
        "select q.id, q.run_id, q.case_id, q.field_key, q.options, q.allow_unknown,"
        " r.status as run_status, r.superseded_at, r.fact_card_version"
        " from public.judgment_questions q join public.judgment_runs r on r.id = q.run_id"
        " where q.id = %s",
        (question_id,),
    )


async def insert_answer(
    conn: AsyncConnection, question: Row, answer: QuestionAnswer, actor_id: str
) -> bool:
    """응답 저장. 이미 답한 질문이면 False(질문마다 한 번)."""
    row = await _one(
        conn,
        "insert into public.judgment_answers"
        " (question_id, case_id, expected_card_version, value, unknown, answered_by)"
        " values (%s, %s, %s, %s, %s, %s) on conflict do nothing returning question_id",
        (question["id"], question["case_id"], answer["expected_card_version"], answer["value"],
         answer["unknown"], actor_id),
    )
    return row is not None


async def run_questions(conn: AsyncConnection, run_id: str) -> list[Row]:
    """실행의 질문과 응답(없으면 answered=False)."""
    cur = await conn.execute(
        "select q.id, q.field_key, a.value, a.unknown, a.expected_card_version,"
        " (a.question_id is not null) as answered"
        " from public.judgment_questions q"
        " left join public.judgment_answers a on a.question_id = q.id"
        " where q.run_id = %s order by q.field_key",
        (run_id,),
    )
    return await cur.fetchall()  # type: ignore[return-value]


async def question_views(conn: AsyncConnection, run_id: str) -> list[Row]:
    """판단 조회용: 실행의 질문 전체(문구·보기 포함)와 답(없으면 answered=False).

    순서는 run_questions와 같다.
    """
    cur = await conn.execute(
        "select q.id, q.field_key, q.text, q.options, q.allow_unknown,"
        " a.value, a.unknown, a.expected_card_version, a.resulting_card_version, a.answered_at,"
        " (a.question_id is not null) as answered"
        " from public.judgment_questions q"
        " left join public.judgment_answers a on a.question_id = q.id"
        " where q.run_id = %s order by q.field_key",
        (run_id,),
    )
    return await cur.fetchall()  # type: ignore[return-value]


async def report_version_for(
    conn: AsyncConnection, case_id: str, judgment_version: int
) -> int | None:
    """그 판단 버전으로 만든 보고서의 최신 버전(D review_reports).

    없거나 테이블이 아직 없으면 None.
    """
    try:
        row = await _one(
            conn,
            "select max(report_version) as version from public.review_reports"
            " where case_id = %s and judgment_version = %s",
            (case_id, judgment_version),
        )
    except UndefinedTable:
        return None
    return row["version"] if row else None


async def claim_resume(conn: AsyncConnection, run_id: str) -> Row | None:
    """확인 대기 → 실행 중. 한 요청만 성공한다(동시 응답이 같은 실행을 두 번 재개하지 않게)."""
    return await _one(
        conn,
        "update public.judgment_runs set status = 'running' where id = %s"
        " and status = 'waiting_for_confirmation' and superseded_at is null"
        f" returning {RUN_COLUMNS}",
        (run_id,),
    )
