"""A: 영상/수동 입력 공통 사실 카드. 모델 원값과 사람의 확인을 분리한다."""

from typing import Literal, TypedDict

FactValue = str | int | float | bool | None


class EvidenceFrame(TypedDict):
    frame_id: str
    timestamp_ms: int
    storage_path: str


class FactField(TypedDict):
    value: FactValue
    model_value: FactValue
    source: Literal["vision", "manual", "statement"]
    model_confidence: float | None
    verification_status: Literal["unverified", "needs_review", "human_confirmed"]
    evidence_frame_ids: list[str]
    source_document_ids: list[str]


class PartyMapping(TypedDict):
    video_ego_party_id: str | None
    insured_party_id: str | None
    chart_party_A_id: str | None
    chart_party_B_id: str | None


class FactCard(TypedDict):
    case_id: str
    version: int
    accident_date: str
    fields: dict[str, FactField]
    party_mapping: PartyMapping
    evidence_frames: list[EvidenceFrame]
    status: Literal["draft", "confirmed"]
    confirmed_by: str | None
    confirmed_at: str | None


class FactPatch(TypedDict):
    expected_version: int
    fields: dict[str, FactField]
    party_mapping: PartyMapping
    reason: str
