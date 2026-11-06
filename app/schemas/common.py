"""B(통합), 전원 검토: ID·버전·작업 상태. 판정 상태와 분리한다."""

from typing import Literal, TypedDict

TaskStatus = Literal["queued", "running", "waiting_for_confirmation", "completed", "failed"]
JudgmentStatus = Literal["draft_ready", "needs_confirmation", "deferred"]


class VersionRef(TypedDict):
    case_id: str
    fact_card_version: int
    judgment_version: int | None
    report_version: int | None


class TaskView(TypedDict):
    task_id: str
    case_id: str
    stage: str
    status: TaskStatus
    input_version: int


class ApiError(TypedDict):
    code: str
    message: str
    retryable: bool
