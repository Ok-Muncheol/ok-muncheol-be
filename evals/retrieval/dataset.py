"""C: 사고·원문/개정판 계열 단위로 분할한 검색 평가셋 계약."""

from typing import Literal, TypedDict

from app.schemas.retrieval import SearchRequest


class RetrievalExample(TypedDict):
    example_id: str
    request: SearchRequest
    expected_parent_ids: list[str]
    expected_edition: str | None
    split_group_id: str
    split: Literal["unassigned", "train", "dev", "test"]
    label_status: Literal["unlabelled", "machine_reviewed", "reviewed"]
    synthetic: bool
    notes: str
