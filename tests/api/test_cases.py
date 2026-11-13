"""A: 사건 접수·조회 — app/api/cases.py create_case·list_cases·get_case·summarize.

DB 없이 메모리 저장소(cases.configure)로 확인한다. SUMMARY_SQL은 Docker Postgres로 따로 확인했다.
- 접수: 차대차 2대만(422 case_out_of_scope), input_path·날짜 검사, 응답은 CaseSummary(stage=intake)
- 조회: 다른 담당자 403, 없는 사건 404, 목록은 자기 사건만(대물·대인 담당자 모두 자기 사건이다)
- 단계: 보고서 → report, 최신 카드의 판단 → judgment, 카드 → facts, 없으면 intake
"""

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from app.api import cases
from app.api.errors import ApiException

ACTOR, OTHER = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
INJURY = "55555555-5555-5555-5555-555555555555"  # 대인 담당자
T0 = datetime(2026, 11, 11, 6, 0, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


def row(**kw) -> dict:
    """SUMMARY_SQL 한 행. 기본은 산출물 없는 사건."""
    base = {
        "id": "33333333-3333-3333-3333-333333333333", "assignee_id": ACTOR,
        "injury_assignee_id": None,
        "accident_date": None, "input_path": "video", "created_at": T0, "updated_at": T0,
        "card_version": None, "card_at": None, "judgment_version": None,
        "run_card_version": None, "run_status": None, "judgment_status": None, "run_at": None,
        "open_questions": 0, "report_version": None, "report_judgment_version": None,
        "approval_status": None, "report_at": None, "analysis_status": None,
        "analysis_at": None, "analysis_updated_at": None,
    }
    return {**base, **kw}


class MemoryCaseRepo:
    def __init__(self):
        self.rows: dict[str, dict] = {}

    async def insert_case(self, actor_id, accident_date, input_path):
        case_id = str(uuid.uuid4())
        self.rows[case_id] = row(id=case_id, assignee_id=actor_id, accident_date=accident_date,
                                 input_path=input_path)
        return case_id

    async def expire_analysis(self, case_id):
        pass

    async def summaries(self, *, case_id=None, assignee_id=None):
        if case_id is not None:
            return [self.rows[case_id]] if case_id in self.rows else []
        return [r for r in self.rows.values()
                if assignee_id in (r["assignee_id"], r["injury_assignee_id"])]


@pytest.fixture
def repo():
    store = MemoryCaseRepo()

    @asynccontextmanager
    async def connect():
        yield None

    old = cases.services
    cases.configure(connect=connect, repo=lambda conn: store)
    yield store
    cases.services = old


def error(exc_info) -> tuple[int, str]:
    return exc_info.value.status_code, exc_info.value.error["code"]


# ---------------------------------------------------------------- 접수


def test_create_case_returns_intake_summary(repo):
    out = run(cases.create_case(ACTOR, {"accident_date": "2026-11-08", "input_path": "video"}))
    assert out["stage"] == "intake" and out["input_path"] == "video"
    assert out["accident_date"] == "2026-11-08T00:00:00+09:00"  # 시간대 없으면 한국 시간
    assert out["versions"] == {"case_id": out["case_id"], "fact_card_version": 0,
                               "judgment_version": None, "report_version": None}
    assert repo.rows[out["case_id"]]["assignee_id"] == ACTOR


def test_create_case_defaults(repo):
    out = run(cases.create_case(ACTOR, {}))
    assert out["input_path"] == "video" and out["accident_date"] is None


@pytest.mark.parametrize(
    ("payload", "code"),
    [({"accident_type": "car_to_pedestrian"}, "case_out_of_scope"),
     ({"party_count": 3}, "case_out_of_scope"),
     ({"party_count": 1}, "case_out_of_scope"),
     ({"party_count": True}, "case_out_of_scope"),
     ({"input_path": "cctv"}, "invalid_case"),
     ({"accident_date": "어제"}, "invalid_case")],
)
def test_create_case_rejects(repo, payload, code):
    with pytest.raises(ApiException) as e:
        run(cases.create_case(ACTOR, payload))
    assert error(e) == (422, code)
    assert not repo.rows  # 범위 밖·잘못된 요청은 사건을 만들지 않는다


# ---------------------------------------------------------------- 조회


def test_get_and_list_check_assignee(repo):
    mine = run(cases.create_case(ACTOR, {}))
    run(cases.create_case(OTHER, {}))
    assert run(cases.get_case(ACTOR, mine["case_id"]))["case_id"] == mine["case_id"]
    with pytest.raises(ApiException) as e:
        run(cases.get_case(OTHER, mine["case_id"]))
    assert error(e) == (403, "forbidden")
    for missing in (str(uuid.uuid4()), "not-a-uuid"):
        with pytest.raises(ApiException) as e:
            run(cases.get_case(ACTOR, missing))
        assert error(e) == (404, "not_found")
    listed = run(cases.list_cases(ACTOR))
    assert [c["case_id"] for c in listed] == [mine["case_id"]]
    assert listed[0]["created_at"] == listed[0]["updated_at"]  # 배정 시각을 함께 준다(+09:00)


def test_injury_assignee_gets_and_lists_the_case(repo):
    """대인 담당자(cases.injury_assignee_id)도 사건을 조회하고 자기 목록에서 본다."""
    mine = run(cases.create_case(ACTOR, {}))
    repo.rows[mine["case_id"]]["injury_assignee_id"] = INJURY
    assert run(cases.get_case(INJURY, mine["case_id"]))["case_id"] == mine["case_id"]
    assert [c["case_id"] for c in run(cases.list_cases(INJURY))] == [mine["case_id"]]
    assert [c["case_id"] for c in run(cases.list_cases(ACTOR))] == [mine["case_id"]]
    with pytest.raises(ApiException) as e:
        run(cases.get_case(OTHER, mine["case_id"]))
    assert error(e) == (403, "forbidden")
    assert run(cases.list_cases(OTHER)) == []


# ---------------------------------------------------------------- 단계


def test_stage_facts_when_card_only():
    out = cases.summarize(row(card_version=2, card_at=T0))
    assert (out["stage"], out["task_status"]) == ("facts", None)
    assert out["versions"]["fact_card_version"] == 2


def test_stage_judgment_with_open_questions():
    out = cases.summarize(row(card_version=2, judgment_version=1, run_card_version=2,
                              run_status="waiting_for_confirmation", open_questions=2))
    assert (out["stage"], out["task_status"], out["open_questions"]) == (
        "judgment", "waiting_for_confirmation", 2)
    assert out["versions"]["judgment_version"] == 1


def test_stage_back_to_facts_after_card_edit():
    """질문에 답해 카드가 새 버전이 되면 이전 판단은 지금 단계가 아니다."""
    out = cases.summarize(row(card_version=3, judgment_version=1, run_card_version=2,
                              run_status="waiting_for_confirmation", open_questions=1))
    assert out["stage"] == "facts" and out["versions"]["judgment_version"] is None
    assert out["open_questions"] == 0


def test_stage_report_and_updated_at():
    later = datetime(2026, 11, 11, 7, 0, tzinfo=UTC)
    out = cases.summarize(row(card_version=2, judgment_version=1, run_card_version=2,
                              run_status="completed", judgment_status="draft_ready",
                              report_version=1, report_judgment_version=1,
                              approval_status="draft", report_at=later))
    assert (out["stage"], out["judgment_status"], out["approval_status"]) == (
        "report", "draft_ready", "draft")
    assert out["versions"]["report_version"] == 1
    assert out["updated_at"] == "2026-11-11T16:00:00+09:00"


def test_old_report_not_current():
    out = cases.summarize(row(card_version=2, judgment_version=2, run_card_version=2,
                              run_status="running", report_version=1, report_judgment_version=1))
    assert out["stage"] == "judgment" and out["versions"]["report_version"] is None


def test_stage_analysis_running_and_failed():
    out = cases.summarize(row(analysis_status="running", analysis_at=T0, analysis_updated_at=T0))
    assert (out["stage"], out["task_status"]) == ("analysis", "running")
    out = cases.summarize(row(analysis_status="failed", analysis_at=T0))
    assert (out["stage"], out["task_status"]) == ("analysis", "failed")


def test_stage_facts_after_analysis_completed_or_older_failure():
    later = datetime(2026, 11, 11, 7, 0, tzinfo=UTC)
    done = cases.summarize(row(analysis_status="completed", analysis_at=T0, card_version=1,
                               card_at=later))
    assert done["stage"] == "facts"
    old_fail = cases.summarize(row(analysis_status="failed", analysis_at=T0, card_version=1,
                                   card_at=later))
    assert old_fail["stage"] == "facts"
    new_fail = cases.summarize(row(analysis_status="failed", analysis_at=later, card_version=1,
                                   card_at=T0))
    assert (new_fail["stage"], new_fail["task_status"]) == ("analysis", "failed")
