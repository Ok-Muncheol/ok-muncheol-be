"""시연용 제휴 병원 수신 가데이터: 사건마다 다르고 같은 사건은 늘 같다. DB 없이 돈다."""

from datetime import UTC, datetime

from app.api import partner_medical as pm
from app.api.reports import validate_documents

CASE_A = "6c96b43e-c9d2-4602-bab9-7b21acdd2975"
CASE_B = "8fd749f6-0000-4000-8000-000000000001"
AT = datetime(2026, 11, 9, 23, 40, tzinfo=UTC)  # KST 11/10 08:40


def test_records_are_stable_per_case_and_differ_between_cases():
    first = pm.records(CASE_A, 2, AT)
    assert first == pm.records(CASE_A, 2, AT)
    assert [r["person_id"] for r in first] == ["partner-1", "partner-2"]
    assert (first[0]["label"], first[0]["is_driver"]) == ("상대 운전자", True)
    assert (first[1]["label"], first[1]["is_driver"]) == ("상대 동승자 1", False)
    other = pm.records(CASE_B, 2, AT)
    assert [(r["hospital"], r["treatment_cost"]) for r in other] != [
        (r["hospital"], r["treatment_cost"]) for r in first]


def test_records_have_plausible_values():
    for case_id in (CASE_A, CASE_B):
        for r in pm.records(case_id, 3, AT):
            assert r["hospital"] in pm.HOSPITALS and r["source"] == pm.SOURCE
            assert 1 <= r["grade"] <= 14 and r["treatment_cost"] % 100 == 0
            assert 200_000 <= r["treatment_cost"] <= 5_200_000
            # 진료는 사고 날(KST)에 시작해 그 뒤에 끝난다
            assert r["treated_from"] == "2026-11-10" and r["treated_to"] > r["treated_from"]
    assert pm.records(CASE_A, 0, AT) == []
    assert len(pm.records(CASE_A, 1, None)) == 1  # 사고 일시를 몰라도 만든다


def test_a_record_fits_the_injured_person_input():
    r = pm.records(CASE_A, 1, AT)[0]
    doc = {"kind": "injured_person", "document_id": "injury-1", "person_id": r["person_id"],
           "label": r["label"], "grade": r["grade"], "treatment_cost": r["treatment_cost"],
           "is_driver": r["is_driver"], "received_at": r["received_at"]}
    validate_documents([doc])  # 대인 입력 검사를 그대로 통과한다


def test_injured_count_follows_the_intake_or_the_injury_assignee():
    assert pm.injured_count({"injured_count": 2, "injury_assignee_id": None}) == 2
    assert pm.injured_count({"injured_count": 0, "injury_assignee_id": "u"}) == 1
    assert pm.injured_count({"injured_count": None, "injury_assignee_id": None}) == 0
