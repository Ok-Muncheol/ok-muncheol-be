"""B(통합), 전원 검토: ID·버전·작업 상태·업로드 표. 판정 상태와 분리한다."""

from typing import Literal, TypedDict

TaskStatus = Literal["queued", "running", "waiting_for_confirmation", "completed", "failed"]
JudgmentStatus = Literal["draft_ready", "needs_confirmation", "deferred"]
# 업로드 파일 종류. 형식 검사는 A 서비스(cases.request_upload)가 한다 — estimate는 application/pdf,
# video는 mp4·mov·avi·webm·mkv. 아니면 422 invalid_upload_type
UploadKind = Literal["estimate", "video"]


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


class UploadTicket(TypedDict):
    """`POST /cases/{id}/uploads` 응답. 파일은 FE가 Storage에 직접 올린다.

    FE는 `upload_url`에 키 없이 파일 본문과 Content-Type만 넣어 PUT하고,
    `storage_path`를 `/estimates`(견적서)나 `/analysis`(영상)에 보낸다.
    """

    # 버킷 포함: case-files/{case_id}/estimates/{uuid}.pdf
    #            case-files/{case_id}/videos/{uuid}.{확장자}
    storage_path: str
    upload_url: str  # Supabase 서명 업로드 URL(절대 주소, 한 번만 쓸 수 있음)
    expires_in: int  # 초. Supabase 서명 업로드 URL은 7200초
