"""D 테스트용 합성 입력. 실제 사고·도표·약관·금액이 아니다(모든 ID에 synthetic/example 표시)."""

from contextlib import contextmanager

from app.api.errors import ApiException
from app.schemas.facts import FactCard, FactField
from app.schemas.judgment import JudgmentResult
from app.schemas.retrieval import EvidenceHit


def _field(value: object, confirmed: bool = True, model_value: object = None) -> FactField:
    return {
        "value": value,
        "model_value": model_value,
        "source": "manual",
        "model_confidence": None,
        "verification_status": "human_confirmed" if confirmed else "needs_review",
        "evidence_frame_ids": [],
        "source_document_ids": ["synthetic-statement"],
    }


def card(insured: str = "P1") -> FactCard:
    return {
        "case_id": "synthetic-case",
        "version": 2,
        "accident_date": "2026-10-20",
        "fields": {
            "accident_type": _field("car_to_car"),
            "location_type": _field("signalized_intersection"),
            "signal_A": _field("green", model_value="yellow"),
            "signal_B": _field("red"),
            "movement_A": _field("straight"),
            "movement_B": _field("straight"),
        },
        "party_mapping": {
            "video_ego_party_id": "P1",
            "insured_party_id": insured,
            "chart_party_A_id": "P1",
            "chart_party_B_id": "P2",
        },
        "evidence_frames": [
            {"frame_id": "f-062", "timestamp_ms": 6200, "storage_path": "synthetic/f-062.jpg"}
        ],
        "status": "confirmed",
        "confirmed_by": "handler-1",
        "confirmed_at": "2026-11-05T09:00:00+09:00",
    }


def _hit(corpus: str, parent: str, text: str) -> EvidenceHit:
    return {
        "chunk": {
            "chunk_id": parent + "#0",
            "parent_id": parent,
            "document": {
                "document_id": f"synthetic-{corpus}-doc",
                "corpus": corpus,
                "source_uri": "synthetic",
                "edition": "synthetic-2023",
                "effective_from": "2023-06-01",
                "effective_to": None,
                "content_hash": "x",
                "parser_version": "p0",
            },
            "page_start": 112,
            "page_end": 112,
            "text": text,
            "metadata": {},
        },
        "score": 0.9,
        "retrieval_version": "r0",
    }


def judgment(
    status: str = "draft_ready", judgment_version: int = 1, fault_A: int = 30
) -> JudgmentResult:
    ready = status == "draft_ready"
    j: dict = {
        "versions": {
            "case_id": "synthetic-case",
            "fact_card_version": 2,
            "judgment_version": judgment_version,
            "report_version": None,
        },
        "status": status,
        "chart_id": "synthetic-chart-23" if status != "deferred" else None,
        "edition": "synthetic-2023" if status != "deferred" else None,
        "fault_A": fault_A if ready else None,
        "fault_B": 100 - fault_A if ready else None,
        "calculation_log": [f"base A{fault_A} + 0 - 0 = {fault_A}"] if ready else [],
        "evidence": [
            _hit(
                "fault_standards", "synthetic-chart-23", "(합성) 신호교차로 녹색 직진 대 적색 직진"
            ),
            _hit("dispute_cases", "synthetic-dispute-0412", "(합성) 유사 결정례 30:70"),
        ],
        "checks": [
            {"layer": "evidence", "passed": True, "reasons": []},
            {
                "layer": "rebuttal",
                "passed": ready,
                "reasons": [] if ready else ["선진입 여부 불명"],
            },
            {"layer": "numerical", "passed": True, "reasons": []},
        ],
        "questions": []
        if ready
        else [
            {
                "question_id": "q1",
                "field_key": "first_entry",
                "text": "먼저 진입한 차량은?",
                "options": ["A", "B"],
                "allow_unknown": True,
            }
        ],
        "reasons": [] if ready else ["선진입 여부 확인 필요"],
        # B 계약 제안 필드 (있으면 보고서 3~5번을 채움)
        "base_fault_A": fault_A,
        "modifiers": [
            {
                "modifier_id": "synthetic-M-night",
                "name": "야간",
                "applies_to": "B",
                "delta": 5,
                "decision": "미적용",
                "evidence_fields": ["time_condition"],
            }
        ],
        "alternatives": [
            {"chart_id": "synthetic-chart-24", "reason": "B 신호가 황색이 아니라 적색"}
        ],
    }
    return j  # type: ignore[return-value]


def contract(**kw: object) -> dict[str, object]:
    d: dict[str, object] = {
        "kind": "own_damage_contract",
        "document_id": "synthetic-contract",
        "policy_id": "synthetic-policy",
        "own_damage_joined": True,
        "period_start": "2026-03-01",
        "period_end": "2027-02-28",
        "insured_amount": 25_000_000,
        "vehicle_value": 25_000_000,
        "deductible_fixed": None,
        "deductible_rate": None,
        "deductible_min": None,
        "deductible_max": None,
        "driver_scope": "부부 한정",
        "driver_in_scope": True,
        "terms_edition": "synthetic-terms-2026",
    }
    d.update(kw)
    return d


def estimate(
    amount: int | None = 10_000_000, doc_id: str = "synthetic-est-1", **kw: object
) -> dict[str, object]:
    d: dict[str, object] = {
        "kind": "repair_estimate",
        "document_id": doc_id,
        "party_id": "P1",
        "amount": amount,
        "read_by": "manual",
        "verification_status": "human_confirmed",
        "received_at": "2026-10-25T10:00:00+09:00",
        "storage_path": f"synthetic/{doc_id}.pdf",
    }
    d.update(kw)
    return d


def evidence(prefix: str = "synthetic-ev") -> list[dict[str, object]]:
    """사건에 바로 연결하는 증빙 3건(수리 명세·영수증·사진)."""
    return [
        {
            "kind": "evidence",
            "document_id": f"{prefix}-{i}",
            "doc_type": t,
            "storage_path": f"synthetic/{prefix}{i}",
        }
        for i, t in enumerate(["repair_invoice", "receipt", "photo"], 1)
    ]


def counterparty() -> dict[str, object]:
    return {
        "vehicle_no": "synthetic-12가3456",
        "insurer": "synthetic-insurer",
        "claim_no": "synthetic-cp-998",
    }


NO_EXCLUSION = [
    {
        "key": f"exclusion:{k}",
        "label": k,
        "result": "pass",
        "decided_by": "code",
        "detail": "",
        "clause_ids": [],
    }
    for k in ("무면허", "음주", "고의")
]


@contextmanager
def raises_api(test, status: int, code: str | None = None, message: str | None = None):
    """D 서비스 함수가 ApiException(status·code)을 던지는지. message는 메시지에 들어갈 문구."""
    with test.assertRaises(ApiException) as ctx:
        yield ctx
    error = ctx.exception.error
    test.assertEqual(ctx.exception.status_code, status, error)
    if code is not None:
        test.assertEqual(error["code"], code)
    if message is not None:
        test.assertIn(message, error["message"])
