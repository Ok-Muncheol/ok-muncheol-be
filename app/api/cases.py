"""A: POST /cases, /uploads, /analysis, GET /cases/{id} 예정."""


async def create_case(actor_id: str, payload: dict[str, object]) -> dict[str, object]:
    """사고 범위·당사자 수 검사, 담당 사건 소유권과 초기 상태 저장."""
    raise NotImplementedError("A: 사건 접수·권한·범위 검사")


async def request_upload(actor_id: str, case_id: str) -> dict[str, object]:
    """담당 사건에 한해 Storage 직접 업로드용 서명 URL 발급."""
    raise NotImplementedError("A: 업로드 서명 URL·크기/형식/만료 검사")


async def start_analysis(
    actor_id: str, case_id: str, storage_path: str, idempotency_key: str
) -> dict[str, object]:
    """소유권·업로드 완료 확인 후 비전 작업 큐에 접수."""
    raise NotImplementedError("A: 작업 접수·멱등성·상태 복원")


async def get_case(actor_id: str, case_id: str) -> dict[str, object]:
    """현재 작업·카드·질문·보고서 버전을 권한 검사 후 조회."""
    raise NotImplementedError("A: 사건 조회·복원")
