"""A: 사실 카드 조회·수정·확정 — app/api/facts.py.

DB 없이 메모리 저장소(facts.configure)로 확인한다. SQL 표 제약은 tests/migrations/behavior.sql 8절.
- 권한: 다른 담당자 403, 없는 사건 404, 카드 없으면 404
- 수정: 기대 버전 409, 값 검사 422, 사유 필수, 고친 칸 human_confirmed·모델 원값 보존, 새 버전 draft
- 확정: needs_review·매핑 문제면 422, 값 있는 칸은 human_confirmed, 같은 확정 다시 보내도 같은 결과
- 비전 카드 저장: 버전이 1부터 늘고 늘 draft
"""

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from app.api import facts
from app.api.errors import ApiException
from app.schemas.facts import FACT_FIELDS, FactCard

ACTOR, OTHER = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
CASE = "33333333-3333-3333-3333-333333333333"
EXAMPLE = Path(__file__).resolve().parents[2] / "docs/contracts/fact-card-v1.example.json"
NOW = "2026-11-11T15:00:00+00:00"


def run(coro):
    return asyncio.run(coro)


class MemoryRepo:
    def __init__(self, assignees):
        self.assignees = assignees
        self.rows: list[dict] = []  # {"card", "origin", "reason", "actor"}

    async def assignee(self, case_id):
        return self.assignees.get(case_id)

    async def latest(self, case_id):
        rows = [r for r in self.rows if r["card"]["case_id"] == case_id]
        if not rows:
            return None
        return copy.deepcopy(max(rows, key=lambda r: r["card"]["version"])["card"])

    async def insert(self, card, origin, reason, actor_id):
        key = (card["case_id"], card["version"])
        if any((r["card"]["case_id"], r["card"]["version"]) == key for r in self.rows):
            return False
        self.rows.append(
            {"card": copy.deepcopy(card), "origin": origin, "reason": reason, "actor": actor_id}
        )
        return True

    async def confirm(self, card):
        for r in self.rows:
            key = (r["card"]["case_id"], r["card"]["version"])
            if key == (card["case_id"], card["version"]) and r["card"]["status"] == "draft":
                r["card"] = copy.deepcopy(card)
                return True
        return False


@pytest.fixture
def repo():
    store = MemoryRepo({CASE: ACTOR})

    @asynccontextmanager
    async def connect():
        yield None

    old = facts.services
    facts.configure(connect=connect, repo=lambda conn: store, now=lambda: NOW)
    yield store
    facts.services = old


def vision_card() -> dict:
    """비전 파이프라인이 낸 v1 카드(예시 파일). 확인 필요: location·road·movement_A·entry·time."""
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def saved(repo) -> dict:
    return run(facts.save_vision_card(CASE, vision_card()))


def error(exc_info) -> tuple[int, str]:
    return exc_info.value.status_code, exc_info.value.error["code"]


def patch(version, fields, reason="영상 다시 봄", mapping=None):
    body = {"expected_version": version, "fields": {k: {"value": v} for k, v in fields.items()},
            "reason": reason}
    if mapping:
        body["party_mapping"] = mapping
    return body


REVIEW_FIX = {  # 예시 카드의 needs_review 칸을 모두 담당자가 정한다(entry_order는 모름)
    "location_type": "unsignalized_intersection", "road_priority": "same_width",
    "movement_A": "straight", "entry_order": None, "collision_time_ms": 4667,
}


# ---------------------------------------------------------------- 저장·조회


def test_save_vision_card_versions_and_get(repo):
    first = saved(repo)
    assert first["version"] == 1 and first["status"] == "draft" and first["case_id"] == CASE
    second = saved(repo)
    assert second["version"] == 2
    assert [r["origin"] for r in repo.rows] == ["vision", "vision"]
    assert repo.rows[0]["actor"] is None
    assert run(facts.get_fact_card(ACTOR, CASE))["version"] == 2


def test_card_passes_contract_type_with_extras():
    """API 응답 검증(FactCard)을 통과하고 vision_detail·parties·model_info가 지워지지 않는다."""
    card = vision_card()
    ta = TypeAdapter(FactCard)
    out = ta.dump_python(ta.validate_python(card))
    assert out["fields"]["location_type"]["vision_detail"]["eval"]["accuracy"] == 0.8
    assert out["parties"] and out["model_info"]
    assert set(card["fields"]) == set(FACT_FIELDS)


@pytest.mark.parametrize(
    ("actor", "case", "code"),
    [(OTHER, CASE, (403, "forbidden")), (ACTOR, "44444444-4444-4444-4444-444444444444",
     (404, "not_found")), (ACTOR, "not-a-uuid", (404, "not_found"))],
)
def test_get_checks_assignee(repo, actor, case, code):
    saved(repo)
    with pytest.raises(ApiException) as e:
        run(facts.get_fact_card(actor, case))
    assert error(e) == code


def test_get_without_card_is_404(repo):
    with pytest.raises(ApiException) as e:
        run(facts.get_fact_card(ACTOR, CASE))
    assert error(e) == (404, "not_found")


# ---------------------------------------------------------------- 수정


def test_patch_makes_new_draft_and_keeps_model_values(repo):
    saved(repo)
    out = run(facts.patch_fact_card(ACTOR, CASE, patch(1, {"relative_position": "left"})))
    f = out["fields"]["relative_position"]
    assert out["version"] == 2 and out["status"] == "draft"
    assert f["value"] == "left" and f["model_value"] == "right" and f["source"] == "manual"
    assert f["verification_status"] == "human_confirmed" and "eval" in f["vision_detail"]
    assert repo.rows[-1]["origin"] == "edit" and repo.rows[-1]["reason"] == "영상 다시 봄"
    # 이전 버전은 그대로
    assert repo.rows[0]["card"]["fields"]["relative_position"]["value"] == "right"


def test_patch_same_value_just_confirms_field(repo):
    saved(repo)
    out = run(facts.patch_fact_card(ACTOR, CASE, patch(1, {"location_type":
                                                            "unsignalized_intersection"})))
    f = out["fields"]["location_type"]
    assert f["verification_status"] == "human_confirmed" and f["source"] == "vision"


def test_patch_unknown_is_null_confirmed(repo):
    saved(repo)
    out = run(facts.patch_fact_card(ACTOR, CASE, patch(1, {"entry_order": None})))
    assert out["fields"]["entry_order"]["value"] is None
    assert out["fields"]["entry_order"]["verification_status"] == "human_confirmed"


def test_patch_old_version_is_conflict(repo):
    saved(repo)
    run(facts.patch_fact_card(ACTOR, CASE, patch(1, {"night": False})))
    with pytest.raises(ApiException) as e:
        run(facts.patch_fact_card(ACTOR, CASE, patch(1, {"night": True})))
    assert error(e) == (409, "version_conflict")


@pytest.mark.parametrize(
    ("fields", "code"),
    [({"road_priority": "major_minor"}, "invalid_fact_value"),  # 11/6 임시 값
     ({"movement_A": "직진"}, "invalid_fact_value"),  # 내부 한국어 값
     ({"night": "true"}, "invalid_fact_value"),
     ({"collision_time_ms": -1}, "invalid_fact_value"),
     ({"accident_object": "car_to_car"}, "invalid_fact_field")],
)
def test_patch_rejects_values_outside_v1(repo, fields, code):
    saved(repo)
    with pytest.raises(ApiException) as e:
        run(facts.patch_fact_card(ACTOR, CASE, patch(1, fields)))
    assert error(e) == (422, code)


def test_patch_requires_reason_and_valid_mapping(repo):
    saved(repo)
    with pytest.raises(ApiException) as e:
        run(facts.patch_fact_card(ACTOR, CASE, patch(1, {"night": True}, reason=" ")))
    assert error(e) == (422, "invalid_patch")
    bad = {"video_ego_party_id": "P1", "insured_party_id": None,
           "chart_party_A_id": "P1", "chart_party_B_id": "P1"}
    with pytest.raises(ApiException) as e:
        run(facts.patch_fact_card(ACTOR, CASE, patch(1, {}, mapping=bad)))
    assert error(e) == (422, "invalid_party_mapping")


def test_patch_swaps_chart_parties(repo):
    saved(repo)
    swap = {"video_ego_party_id": "P1", "insured_party_id": "P1",
            "chart_party_A_id": "P1", "chart_party_B_id": "P2"}
    out = run(facts.patch_fact_card(ACTOR, CASE, patch(1, {}, mapping=swap)))
    assert out["party_mapping"]["chart_party_A_id"] == "P1"


# ---------------------------------------------------------------- 확정


def test_confirm_blocked_while_needs_review(repo):
    saved(repo)
    with pytest.raises(ApiException) as e:
        run(facts.confirm_fact_card(ACTOR, CASE, 1))
    assert error(e) == (422, "card_not_confirmable")
    assert "location_type" in e.value.error["message"]


def test_confirm_marks_valued_fields_confirmed(repo):
    saved(repo)
    run(facts.patch_fact_card(ACTOR, CASE, patch(1, REVIEW_FIX)))
    out = run(facts.confirm_fact_card(ACTOR, CASE, 2))
    assert out["status"] == "confirmed" and out["confirmed_by"] == ACTOR
    assert out["confirmed_at"] == NOW
    statuses = {k: f["verification_status"] for k, f in out["fields"].items()}
    assert statuses["movement_B"] == "human_confirmed"  # 표시 없던 칸도 확정으로 확인됨
    assert statuses["entry_order"] == "human_confirmed"  # 모름으로 확인한 칸
    assert repo.rows[-1]["card"]["status"] == "confirmed"
    # 같은 확정을 다시 보내도 같은 결과, 새 행 없음
    assert run(facts.confirm_fact_card(ACTOR, CASE, 2)) == out and len(repo.rows) == 2


def test_confirm_old_version_and_patch_after_confirm(repo):
    saved(repo)
    run(facts.patch_fact_card(ACTOR, CASE, patch(1, REVIEW_FIX)))
    with pytest.raises(ApiException) as e:
        run(facts.confirm_fact_card(ACTOR, CASE, 1))
    assert error(e) == (409, "version_conflict")
    run(facts.confirm_fact_card(ACTOR, CASE, 2))
    out = run(facts.patch_fact_card(ACTOR, CASE, patch(2, {"night": False})))
    assert out["version"] == 3 and out["status"] == "draft" and out["confirmed_by"] is None


def test_save_rejects_internal_korean_values(repo):
    card = vision_card()
    card["fields"]["movement_A"]["value"] = "추월"
    with pytest.raises(ApiException) as e:
        run(facts.save_vision_card(CASE, card))
    assert error(e) == (422, "invalid_fact_value")


def test_patch_route_accepts_value_only_and_full_field(repo, monkeypatch):
    """실제 API 경로: 고칠 값만 보내도, FactField 전체를 보내도 받는다. 응답에 vision_detail이
    남는다."""
    from fastapi.testclient import TestClient

    from app.config import Settings, get_settings
    from app.main import app

    saved(repo)
    app.dependency_overrides[get_settings] = lambda: Settings(
        app_env="local", on_vercel=False, supabase_url=None, supabase_jwt_secret=None,
        jwt_audience="authenticated", cors_allow_origins=(), dev_actor_id=ACTOR)
    monkeypatch.setattr(app, "dependency_overrides", app.dependency_overrides)
    client = TestClient(app)
    full = {**vision_card()["fields"]["narrative"], "value": "담당자가 고친 설명"}
    body = {"expected_version": 1, "fields": {"night": {"value": True}, "narrative": full},
            "reason": "확인"}
    res = client.patch(f"/cases/{CASE}/fact-card", json=body)
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["version"] == 2 and out["fields"]["night"]["value"] is True
    assert out["fields"]["narrative"]["value"] == "담당자가 고친 설명"
    assert "eval" in out["fields"]["location_type"]["vision_detail"] and out["parties"]
    app.dependency_overrides.clear()
