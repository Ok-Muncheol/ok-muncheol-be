"""A: POST /cases, /uploads, /analysis, GET /cases/{id} 예정."""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from app import db, storage
from app.api.errors import forbidden, not_found, unprocessable
from app.workflow import runs

UploadKind = Literal["video", "estimate"]

# 올릴 수 있는 형식 → 확장자. 버킷(case-files)도 PDF와 video/*만 받는다
UPLOAD_TYPES: dict[str, dict[str, str]] = {
    "estimate": {"application/pdf": "pdf"},
    "video": {
        "video/mp4": "mp4",
        "video/quicktime": "mov",
        "video/x-msvideo": "avi",
        "video/webm": "webm",
        "video/x-matroska": "mkv",
    },
}
UPLOAD_FOLDERS: dict[str, str] = {"estimate": "estimates", "video": "videos"}


@dataclass
class CaseServices:
    """테스트에서 바꿔 끼울 수 있게 DB 연결·서명 URL 발급을 모은다(judgments.py와 같은 방식)."""

    connect: Callable = db.connect
    sign_upload: Callable[[str], Awaitable[str]] = storage.create_signed_upload_url


services = CaseServices()


def configure(**kw: object) -> CaseServices:
    global services
    services = CaseServices(**{**services.__dict__, **kw})  # type: ignore[arg-type]
    return services


def _case_uuid(value: object) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as exc:
        raise not_found("사건을 찾을 수 없습니다.") from exc


async def _require_assignee(actor_id: str, case_id: str) -> None:
    """담당 사건인지 확인한다. 서버 키는 RLS를 건너뛰므로 Storage 작업 전에 반드시 부른다."""
    async with services.connect() as conn:
        assignee = await runs.case_assignee(conn, case_id)
    if assignee is None:
        raise not_found("사건을 찾을 수 없습니다.")
    if assignee != actor_id:
        raise forbidden()


async def create_case(actor_id: str, payload: dict[str, object]) -> dict[str, object]:
    """사고 범위·당사자 수 검사, 담당 사건 소유권과 초기 상태 저장."""
    raise NotImplementedError("A: 사건 접수·권한·범위 검사")


async def request_upload(
    actor_id: str,
    case_id: str,
    kind: UploadKind | None = None,
    content_type: str | None = None,
) -> dict[str, object]:
    """담당 사건에 한해 Storage 직접 업로드용 서명 URL을 발급한다.

    순서: 사건 권한 확인 → 형식 확인 → 경로 만들기 → 서명 URL 발급(바꾸지 않는다).
    경로: `case-files/{case_id}/videos/{uuid}.{확장자}`,
    `case-files/{case_id}/estimates/{uuid}.pdf`.
    파일 이름은 uuid로 만든다(Supabase가 한글 이름을 400 InvalidKey로 거절한다).
    응답의 `storage_path`는 FE가 PUT 한 뒤 받는 `Key`와 같고, 분석 접수·견적서 경로에 그대로 넘긴다.
    """
    case_id = _case_uuid(case_id)
    await _require_assignee(actor_id, case_id)
    types = UPLOAD_TYPES.get(str(kind))
    if types is None:
        raise unprocessable("invalid_upload_type", "kind는 video 또는 estimate여야 합니다.")
    ext = types.get((content_type or "").split(";")[0].strip().lower())
    if ext is None:
        allowed = ", ".join(types)
        message = f"{kind}는 다음 형식만 올릴 수 있습니다: {allowed}"
        raise unprocessable("invalid_upload_type", message)
    object_key = f"{case_id}/{UPLOAD_FOLDERS[str(kind)]}/{uuid.uuid4()}.{ext}"
    upload_url = await services.sign_upload(object_key)
    return {
        "storage_path": storage.storage_path_of(object_key),
        "upload_url": upload_url,
        "expires_in": storage.UPLOAD_URL_EXPIRES_SECONDS,
    }


async def start_analysis(
    actor_id: str, case_id: str, storage_path: str, idempotency_key: str
) -> dict[str, object]:
    """소유권·업로드 완료 확인 후 비전 작업 큐에 접수."""
    raise NotImplementedError("A: 작업 접수·멱등성·상태 복원")


async def get_case(actor_id: str, case_id: str) -> dict[str, object]:
    """현재 작업·카드·질문·보고서 버전을 권한 검사 후 조회."""
    raise NotImplementedError("A: 사건 조회·복원")
