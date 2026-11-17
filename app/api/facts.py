"""A: 사실 카드 조회·수정·확정과 비전 카드 저장. actor_id는 인증 계층에서 받는다.

저장: public.fact_cards(카드 한 버전 = 한 행, supabase/migrations/20261111150000_fact_cards.sql).
칸·값: docs/contracts/fact-card-v1.md. 판단(B)은 확정(confirmed) 카드의 human_confirmed 칸만 쓴다.

- 조회: 담당자 확인 → 최신 버전.
- 수정(PATCH): 기대 버전이 최신이 아니면 409. 고친 칸은 human_confirmed, 모델 원값(model_value·
  model_confidence·vision_detail)은 그대로. 새 버전은 늘 draft(기존 확정을 잇지 않는다). 사유 필수.
  모름은 value null로 고친다(확인한 모름).
- 확정: needs_review 칸이 남았거나 도표 A/B 매핑이 틀리면 422. 통과하면 값이 있는 칸은 모두
  human_confirmed(담당자가 카드 전체를 확인했다는 뜻, 2026-11-11 결정)이고 확정한 사람·시각을
  남긴다.
- 영상 없이 진행(POST): 카드가 없고 영상 분석이 대기·진행 중이 아니면 칸이 모두 빈 카드를
  담당자 이름으로 만든다(origin manual). 비워 둔 칸은 판단이 구분 질문으로 묻는다.
- 지금 카드: 가장 큰 버전. 사건을 접수 단계로 되돌렸으면(cases.restarted_at) 그 뒤에 만든 카드만
  지금 카드다 — 그 전 카드는 이력이고 조회·수정·확정 대상이 아니다. 버전 번호는 이어서 매긴다.
- 판단 단계로 되돌리기(cases.restart_case): 원본 카드(영상 분석 카드·빈 카드)를 확인 전 상태의
  새 버전으로 다시 만든다(pristine_card).
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Protocol

from app import db
from app.api.errors import ApiException, forbidden, not_found, unprocessable, version_conflict
from app.schemas.facts import (
    BOOL_FIELDS,
    FACT_FIELD_OPTIONS,
    FACT_FIELDS,
    FactCard,
    FactField,
    FactPatch,
    Party,
)
from app.workflow import runs

KST = timezone(timedelta(hours=9))


class FactRepo(Protocol):
    async def assignees(self, case_id: str) -> tuple[str, ...] | None: ...
    async def latest(self, case_id: str) -> FactCard | None: ...
    async def next_version(self, case_id: str) -> int: ...
    async def insert(
        self, card: FactCard, origin: str, reason: str | None, actor_id: str | None
    ) -> bool: ...
    async def confirm(self, card: FactCard) -> bool: ...
    async def accident_date(self, case_id: str) -> datetime | None: ...
    async def analysis_active(self, case_id: str) -> bool: ...


class PostgresFactRepo:
    """public.fact_cards. 연결 하나로 한 요청을 처리한다."""

    def __init__(self, conn: Any):
        self.conn = conn

    async def assignees(self, case_id: str) -> tuple[str, ...] | None:
        return await runs.case_assignees(self.conn, case_id)

    async def latest(self, case_id: str) -> FactCard | None:
        """지금 카드. 사건을 접수 단계로 되돌린 뒤에는 그 뒤에 만든 카드만 본다."""
        cur = await self.conn.execute(
            "select f.card from public.fact_cards f join public.cases c on c.id = f.case_id"
            f" where f.case_id = %s and f.created_at >= {runs.RESTARTED_AT_SQL}"
            " order by f.version desc limit 1",
            (case_id,),
        )
        row = await cur.fetchone()
        return row["card"] if row else None

    async def next_version(self, case_id: str) -> int:
        """다음 카드 버전. 이력으로 남은 카드까지 세어 번호가 겹치지 않게 한다."""
        cur = await self.conn.execute(
            "select coalesce(max(version), 0) + 1 as next from public.fact_cards"
            " where case_id = %s",
            (case_id,),
        )
        return int((await cur.fetchone())["next"])

    async def origin(self, case_id: str) -> FactCard | None:
        """지금 카드의 원본 — 영상 분석이 만든 카드나 영상 없이 시작한 빈 카드 가운데 최신."""
        cur = await self.conn.execute(
            "select f.card from public.fact_cards f join public.cases c on c.id = f.case_id"
            f" where f.case_id = %s and f.created_at >= {runs.RESTARTED_AT_SQL}"
            " and f.origin in ('vision', 'manual') order by f.version desc limit 1",
            (case_id,),
        )
        row = await cur.fetchone()
        return row["card"] if row else None

    async def insert(
        self, card: FactCard, origin: str, reason: str | None, actor_id: str | None
    ) -> bool:
        """새 버전 행. 같은 버전이 이미 있으면(동시 수정) False."""
        from psycopg import errors
        from psycopg.types.json import Json

        try:
            await self.conn.execute(
                "insert into public.fact_cards (case_id, version, card, origin, reason, created_by)"
                " values (%s, %s, %s, %s, %s, %s)",
                (card["case_id"], card["version"], Json(card), origin, reason, actor_id),
            )
        except errors.UniqueViolation:
            return False
        return True

    async def confirm(self, card: FactCard) -> bool:
        """draft인 그 버전만 확정한다. 이미 확정됐거나 없으면 False."""
        from psycopg.types.json import Json

        cur = await self.conn.execute(
            "update public.fact_cards set card = %s"
            " where case_id = %s and version = %s and status = 'draft' returning version",
            (Json(card), card["case_id"], card["version"]),
        )
        return await cur.fetchone() is not None

    async def accident_date(self, case_id: str) -> datetime | None:
        cur = await self.conn.execute(
            "select accident_date from public.cases where id = %s", (case_id,)
        )
        row = await cur.fetchone()
        return row["accident_date"] if row else None

    async def analysis_active(self, case_id: str) -> bool:
        """대기·진행 중인 영상 분석이 있는가. 콜백이 15분 넘게 안 온 작업은 먼저 실패로 바꾼다."""
        from psycopg.types.json import Jsonb

        from app.api.analysis import EXPIRE_SQL, TIMEOUT_ERROR

        await self.conn.execute(EXPIRE_SQL, (Jsonb(TIMEOUT_ERROR), case_id))
        cur = await self.conn.execute(
            "select 1 from public.analysis_tasks"
            " where case_id = %s and status in ('queued', 'running') limit 1",
            (case_id,),
        )
        return await cur.fetchone() is not None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class FactServices:
    """테스트에서 바꿔 끼울 수 있게 DB 연결·저장소·시각을 모은다(cases.py와 같은 방식)."""

    connect: Callable = db.connect
    repo: Callable[[Any], FactRepo] = PostgresFactRepo
    now: Callable[[], str] = _now


services = FactServices()


def configure(**kw: object) -> FactServices:
    global services
    services = FactServices(**{**services.__dict__, **kw})  # type: ignore[arg-type]
    return services


# ── 규칙(DB 없음) ────────────────────────────────────────────────


def _case_uuid(value: object) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as exc:
        raise not_found("사건을 찾을 수 없습니다.") from exc


def check_value(key: str, value: object) -> None:
    """v1 칸 이름·값 검사(fact-card-v1.md 3절). null은 모든 칸에서 '모름'."""
    if key not in FACT_FIELDS:
        raise unprocessable("invalid_fact_field", f"사실 카드에 없는 칸입니다: {key}")
    if value is None:
        return
    if key in FACT_FIELD_OPTIONS:
        ok = isinstance(value, str) and value in FACT_FIELD_OPTIONS[key]
    elif key in BOOL_FIELDS:
        ok = isinstance(value, bool)
    elif key == "collision_time_ms":
        ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
    else:  # narrative
        ok = isinstance(value, str)
    if not ok:
        raise unprocessable("invalid_fact_value", f"{key}에 쓸 수 없는 값입니다: {value!r}")


def mapping_problems(card: FactCard) -> list[str]:
    """도표 A/B 매핑 문제. 비었거나 같거나 당사자 목록에 없으면 이유를 돌려준다."""
    mapping = card.get("party_mapping") or {}
    a, b = mapping.get("chart_party_A_id"), mapping.get("chart_party_B_id")
    if not a or not b:
        return ["도표 A·B 당사자가 정해지지 않았습니다."]
    if a == b:
        return ["도표 A와 B가 같은 당사자입니다."]
    ids = {p["party_id"] for p in card.get("parties") or []}
    missing = [x for x in (a, b) if ids and x not in ids]
    return [f"당사자 목록에 없는 ID입니다: {', '.join(missing)}"] if missing else []


def apply_patch(card: FactCard, patch: FactPatch) -> FactCard:
    """최신 카드에 수정을 얹은 새 버전(draft). 원본 카드는 바꾸지 않는다."""
    reason = patch.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise unprocessable("invalid_patch", "수정 사유를 적어 주세요.")
    new: FactCard = copy.deepcopy(card)
    for key, given in (patch.get("fields") or {}).items():
        value = given.get("value") if isinstance(given, dict) else None
        check_value(key, value)
        old = new["fields"].get(key) or {
            "value": None, "model_value": None, "source": "manual", "model_confidence": None,
            "verification_status": "unverified", "evidence_frame_ids": [],
            "source_document_ids": [],
        }
        changed = value != old.get("model_value")
        new["fields"][key] = {
            **old,
            "value": value,
            "source": "manual" if changed else old.get("source", "manual"),
            "verification_status": "human_confirmed",
        }
    if patch.get("party_mapping"):
        new["party_mapping"] = {**new.get("party_mapping", {}), **patch["party_mapping"]}
        if problems := mapping_problems(new):
            raise unprocessable("invalid_party_mapping", problems[0])
    new["version"] = card["version"] + 1
    new["status"] = "draft"
    new["confirmed_by"] = None
    new["confirmed_at"] = None
    return new


def confirm_problems(card: FactCard) -> list[str]:
    """확정을 막는 이유. needs_review 칸, 도표 A/B 매핑."""
    review = [
        k for k, f in card["fields"].items() if f.get("verification_status") == "needs_review"
    ]
    out = [f"확인이 필요한 칸이 남았습니다: {', '.join(review)}"] if review else []
    return out + mapping_problems(card)


def confirmed_card(card: FactCard, actor_id: str, now: str) -> FactCard:
    """확정한 카드. 값이 있는 unverified 칸은 human_confirmed가 된다(담당자가 전체를 확인함).

    가입자는 촬영 차로 맞춘다.
    """
    out: FactCard = copy.deepcopy(card)
    # 가입자는 촬영 차(카드 B)로 고정한다(카드 v1 2절). 이전에 비워 두거나 달리 고른 카드도 맞춘다
    mapping = out.get("party_mapping") or {}
    if mapping.get("video_ego_party_id"):
        mapping["insured_party_id"] = mapping["video_ego_party_id"]
    for f in out["fields"].values():
        if f.get("verification_status") == "unverified" and f.get("value") is not None:
            f["verification_status"] = "human_confirmed"
    out["status"] = "confirmed"
    out["confirmed_by"] = actor_id
    out["confirmed_at"] = now
    return out


# 영상 없이 시작한 카드의 당사자. 가입자 차를 카드 B(1인칭 영상의 촬영 차량 자리, P1)에 두어
# 판단의 도표 A/B 맞바꿈(is_swapped)과 화면의 맞바꾸기가 영상 카드와 같은 규칙으로 돈다.
BLANK_PARTIES: tuple[Party, ...] = (
    {"party_id": "P1", "role": "가입자 차량", "track_id": None},
    {"party_id": "P2", "role": "상대 차량", "track_id": None},
)


def _empty_field() -> FactField:
    return {
        "value": None, "model_value": None, "source": "manual", "model_confidence": None,
        "verification_status": "unverified", "evidence_frame_ids": [], "source_document_ids": [],
    }


def blank_card(case_id: str, accident_date: str | None, version: int = 1) -> FactCard:
    """영상 없이 시작하는 카드(draft). 칸은 모두 값 없음·출처 manual·unverified다.

    확정을 막지 않고, 비워 둔 칸은 판단이 구분 질문으로 묻는다. 도표 A/B는 계약 기본값
    (A = 상대 차 P2, B = 가입자 차 P1)이고 담당자가 맞바꿀 수 있다. 근거 장면은 없다.
    """
    return {
        "case_id": case_id, "version": version, "accident_date": accident_date,
        "fields": {key: _empty_field() for key in FACT_FIELDS},
        "parties": copy.deepcopy(list(BLANK_PARTIES)),
        "party_mapping": {
            "video_ego_party_id": "P1", "insured_party_id": "P1",
            "chart_party_A_id": "P2", "chart_party_B_id": "P1",
        },
        "evidence_frames": [], "status": "draft", "confirmed_by": None, "confirmed_at": None,
    }


# ── 서비스 ───────────────────────────────────────────────────────


async def _require_assignee(repo: FactRepo, actor_id: str, case_id: str) -> None:
    assignees = await repo.assignees(case_id)
    if assignees is None:
        raise not_found("사건을 찾을 수 없습니다.")
    if actor_id not in assignees:
        raise forbidden()


async def _latest_or_404(repo: FactRepo, case_id: str) -> FactCard:
    card = await repo.latest(case_id)
    if card is None:
        raise not_found("사실 카드가 아직 없습니다. 영상 분석이 끝났는지 확인하세요.")
    return card


async def get_fact_card(actor_id: str, case_id: str) -> FactCard:
    case_id = _case_uuid(case_id)
    async with services.connect() as conn:
        repo = services.repo(conn)
        await _require_assignee(repo, actor_id, case_id)
        return await _latest_or_404(repo, case_id)


async def patch_fact_card(actor_id: str, case_id: str, patch: FactPatch) -> FactCard:
    """기대 버전 검사·원값 보존·이력·새 버전 생성. 기존 승인은 승계하지 않는다."""
    case_id = _case_uuid(case_id)
    async with services.connect() as conn:
        repo = services.repo(conn)
        await _require_assignee(repo, actor_id, case_id)
        card = await _latest_or_404(repo, case_id)
        if patch.get("expected_version") != card["version"]:
            raise version_conflict("사실 카드가 그 사이 바뀌었습니다. 최신 카드를 다시 불러오세요.")
        new = apply_patch(card, patch)
        if not await repo.insert(new, "edit", patch["reason"].strip(), actor_id):
            raise version_conflict("다른 수정이 먼저 저장되었습니다. 최신 카드를 다시 불러오세요.")
        return new


async def confirm_fact_card(actor_id: str, case_id: str, expected_version: int) -> FactCard:
    """핵심 불명·충돌·당사자 매핑 검사 후 담당자/시각과 함께 확정."""
    case_id = _case_uuid(case_id)
    async with services.connect() as conn:
        repo = services.repo(conn)
        await _require_assignee(repo, actor_id, case_id)
        card = await _latest_or_404(repo, case_id)
        if expected_version != card["version"]:
            raise version_conflict("사실 카드가 그 사이 바뀌었습니다. 최신 카드를 다시 불러오세요.")
        if card["status"] == "confirmed":
            return card  # 같은 요청을 다시 보내도 같은 결과
        if problems := confirm_problems(card):
            raise unprocessable("card_not_confirmable", " ".join(problems))
        out = confirmed_card(card, actor_id, services.now())
        if not await repo.confirm(out):
            raise version_conflict("다른 요청이 먼저 확정했습니다. 최신 카드를 다시 불러오세요.")
        return out


ORIENTATION_REASON = "판단이 도표 A/B 당사자를 정함"


async def store_orientation(repo: FactRepo, card: FactCard, actor_id: str) -> FactCard:
    """판단이 도표 A/B를 맞바꾼 카드(graph.choose_orientation)를 다음 버전의 확정 카드로 저장한다.

    칸 값과 확인 상태는 그대로다. 판단·보고서·산정이 모두 카드 매핑을 읽으므로 매핑을 카드에 남긴다.
    그사이 다른 수정이 먼저 저장됐으면 버전 충돌이다.
    """
    new: FactCard = copy.deepcopy(card)
    new["version"] = card["version"] + 1
    new["status"] = "confirmed"
    new["confirmed_by"] = actor_id
    new["confirmed_at"] = services.now()
    if not await repo.insert(new, "edit", ORIENTATION_REASON, actor_id):
        raise version_conflict("사실 카드가 그 사이 바뀌었습니다. 최신 카드를 다시 불러오세요.")
    return new


RESTART_REASON = "담당자가 판단 단계부터 다시 하려고 카드를 초기화함"


def pristine_card(origin: FactCard, version: int) -> FactCard:
    """판단 단계의 처음 카드 — 원본 카드를 확인 전 상태의 새 버전(draft)으로 다시 만든다.

    원본 행(영상 분석 카드·빈 카드)은 확정만 겪는다(수정은 새 버전이다). 확정은 값이 있는
    unverified 칸을 human_confirmed로 바꾸고 확정한 사람·시각을 적으니, 그것만 되돌리면 처음
    그대로다. needs_review 칸이 있던 원본은 확정할 수 없어 처음 그대로 남아 있다.
    """
    new: FactCard = copy.deepcopy(origin)
    for field in new["fields"].values():
        if field.get("verification_status") == "human_confirmed":
            field["verification_status"] = "unverified"
    new["version"] = version
    new["status"] = "draft"
    new["confirmed_by"] = None
    new["confirmed_at"] = None
    return new


def _card_exists() -> ApiException:
    message = "이미 사실 카드가 있습니다. 판단 화면에서 이어서 진행하세요."
    return ApiException(409, "fact_card_exists", message, False)


async def start_without_video(actor_id: str, case_id: str) -> FactCard:
    """블랙박스 영상 없이 진행한다. 카드가 없고 분석이 대기·진행 중이 아니면 빈 카드 v1을 만든다.

    담당자는 판단 화면에서 진술로 칸을 고치고 확정한다. 나중에 영상을 올리면 분석 카드가 다음
    버전으로 들어가고(store_vision_card), 그 전 판단은 이전 카드의 것이라 사실 카드 단계로 돌아간다.
    """
    case_id = _case_uuid(case_id)
    async with services.connect() as conn:
        repo = services.repo(conn)
        await _require_assignee(repo, actor_id, case_id)
        if await repo.latest(case_id) is not None:
            raise _card_exists()
        if await repo.analysis_active(case_id):
            raise ApiException(
                409, "analysis_in_progress",
                "영상 분석이 진행 중입니다. 분석이 끝나면 판단 화면에서 이어서 진행하세요.", False,
            )
        accident = await repo.accident_date(case_id)
        card = blank_card(
            case_id, accident.astimezone(KST).isoformat() if accident else None,
            await repo.next_version(case_id),
        )
        if not await repo.insert(card, "manual", None, actor_id):
            raise _card_exists()  # 같은 요청이 동시에 와서 다른 쪽이 먼저 만들었다
        return card


async def store_vision_card(repo: FactRepo, case_id: str, card: FactCard) -> FactCard:
    """비전 카드를 새 버전(draft)으로 저장한다. 연결·트랜잭션은 부르는 쪽 것을 쓴다.

    분석 콜백(app/api/analysis.py)이 작업 완료 기록과 같은 트랜잭션에서 부른다.
    """
    if not isinstance(card, dict) or not isinstance(card.get("fields"), dict):
        raise unprocessable("invalid_fact_card", "사실 카드 모양이 아닙니다.")
    for key, field in card["fields"].items():
        check_value(key, field.get("value") if isinstance(field, dict) else field)
    new: FactCard = {
        **copy.deepcopy(card),
        "case_id": case_id,
        "version": await repo.next_version(case_id),
        "status": "draft",
        "confirmed_by": None,
        "confirmed_at": None,
    }
    if not await repo.insert(new, "vision", None, None):
        raise version_conflict("같은 사건의 카드가 동시에 저장되었습니다. 다시 시도하세요.")
    return new


async def save_vision_card(case_id: str, card: FactCard) -> FactCard:
    """비전 파이프라인 카드(workers/vision/pipeline.analyze_video 결과)를 새 버전으로 저장한다.

    권한 확인은 작업을 접수할 때 한다. 로컬 실행·시험용.
    서비스 흐름은 분석 콜백이 store_vision_card를 쓴다.
    """
    case_id = _case_uuid(case_id)
    async with services.connect() as conn:
        repo = services.repo(conn)
        if await repo.assignees(case_id) is None:
            raise not_found("사건을 찾을 수 없습니다.")
        return await store_vision_card(repo, case_id, card)
