"""B(인프라): 영상 분석 접수를 Modal 웹 엔드포인트로 넘긴다.

정지원 `analysis.dispatch` 자리에 끼우는 함수다(11/11 합의).
- Modal에는 서버 키(Supabase·DB)를 두지 않는다. 넘기는 값은 `task_id`·`case_id`·
  `video_url`(서명 내려받기, 1시간)·`callback_url`·`callback_token`(작업별 일회용,
  BE는 SHA-256만 저장)뿐이다. 결과는 Modal이 `callback_url`
  (`POST /internal/analysis/{task_id}`, `app/api/routes.py`)로 다시 보낸다.
- Modal 엔드포인트는 프록시 인증으로 막는다: `Modal-Key`·`Modal-Secret` 헤더(`MODAL_PROXY_KEY`·
  `MODAL_PROXY_SECRET`). 엔드포인트는 spawn만 하고 `{"call_id": "..."}`를 바로 돌려준다.
- 설정(`MODAL_ANALYZE_URL`·키·비밀·콜백 주소)이 다 있을 때만 `app/main.py`가 끼운다. 없으면 접수는
  `analysis_not_configured`로 실패한다(`analysis.start_analysis`).
- 서명 URL·토큰·키는 로그·오류 메시지·예외 연결(`__cause__`)에 싣지 않는다.
"""

from typing import Any

import httpx

from app.api.errors import ApiException
from app.config import Settings, get_settings

TIMEOUT_SECONDS = 20.0


def is_configured(settings: Settings) -> bool:
    return bool(
        settings.modal_analyze_url and settings.modal_proxy_key and settings.modal_proxy_secret
        and settings.public_backend_url
    )


def _failed(retryable: bool) -> ApiException:
    return ApiException(
        503, "analysis_dispatch_failed", "영상 분석 서버(Modal)를 부르지 못했습니다.", retryable
    )


async def dispatch_to_modal(
    job: dict[str, Any], *, transport: httpx.AsyncBaseTransport | None = None
) -> str:
    """Modal 엔드포인트를 불러 분석을 시작하고 call_id를 돌려준다. `transport`는 테스트용.

    실패는 ApiException(503 `analysis_dispatch_failed`)이고 `start_analysis`가 작업 failed로 남긴다.
    연결 실패·5xx·429는 다시 접수할 수 있고(retryable), 인증·요청 오류와 이상한 응답은 아니다.
    """
    settings = get_settings()
    if not is_configured(settings):
        raise ApiException(
            503, "analysis_not_configured", "영상 분석 서버(Modal) 설정이 없습니다.", False
        )
    payload = {
        "task_id": job["task_id"],
        "case_id": job["case_id"],
        "video_url": job["video_url"],
        "callback_url": f"{settings.public_backend_url}{job['callback_path']}",
        "callback_token": job["callback_token"],
    }
    headers = {
        "Modal-Key": settings.modal_proxy_key or "",
        "Modal-Secret": settings.modal_proxy_secret or "",
    }
    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS, transport=transport, follow_redirects=False
        ) as client:
            resp = await client.post(
                settings.modal_analyze_url or "", json=payload, headers=headers
            )
    except httpx.HTTPError:
        # 예외 연결을 끊는다(요청 본문의 서명 URL·토큰이 추적 정보에 남지 않게)
        raise _failed(retryable=True) from None
    if not 200 <= resp.status_code < 300:
        raise _failed(retryable=resp.status_code >= 500 or resp.status_code == 429)
    try:
        call_id = resp.json().get("call_id")
    except (ValueError, AttributeError):
        call_id = None
    if not isinstance(call_id, str) or not call_id:
        raise _failed(retryable=False)
    return call_id
