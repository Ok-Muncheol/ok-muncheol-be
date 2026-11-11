"""공용: Supabase Storage에서 서버 키로 파일 하나를 내려받는다.

- 인증은 `apikey` 헤더 하나(`SUPABASE_SECRET_KEY`). 서버 키는 RLS를 건너뛰므로
  **호출하는 쪽이 먼저 사건 권한과 경로를 확인한다**(예: app/api/reports.py
  `upload_estimate_from_storage`).
- 버킷은 `STORAGE_BUCKET` 상수 하나(`case-files`, 11/11 확정 — 비공개, PDF·video/*만).
  Storage 경로는 버킷을 포함한 `<버킷>/<객체 키>` 모양이고, API 호출에는 버킷을 뗀 객체 키를 쓴다.
- `SUPABASE_URL`이나 `SUPABASE_SECRET_KEY`가 없으면 503 `storage_not_configured`
  (app/db.py의 `db_not_configured`와 같은 방식). 연결 실패·Storage 오류는 503 `storage_unavailable`.
- 키 값은 로그·오류 메시지·예외 연결(`__cause__`)에 싣지 않는다.
- 파일 없음·크기 초과는 내부 예외로 올린다. 업무 오류 코드는 호출하는 쪽이 정한다.
"""

import json
from pathlib import Path
from urllib.parse import quote

import httpx

from app.api.errors import ApiException
from app.config import get_settings

STORAGE_BUCKET = "case-files"
TIMEOUT_SECONDS = 15.0
_ERROR_BODY_LIMIT = 4096


class StorageObjectNotFound(LookupError):
    """Storage에 그 경로의 파일이 없다."""


class StorageObjectTooLarge(ValueError):
    """파일이 허용 크기를 넘는다(내려받기는 그 자리에서 멈춘다)."""


def storage_path_of(object_key: str) -> str:
    """객체 키 → 버킷을 포함한 Storage 경로."""
    return f"{STORAGE_BUCKET}/{object_key}"


def _not_configured() -> ApiException:
    return ApiException(503, "storage_not_configured", "서버 Storage 설정이 없습니다.")


def _unavailable(retryable: bool) -> ApiException:
    return ApiException(
        503, "storage_unavailable", "Storage에서 파일을 받지 못했습니다.", retryable
    )


def _is_not_found(status_code: int, body: bytes) -> bool:
    """Supabase Storage는 버전에 따라 없는 파일을 404 또는 400 + 본문 statusCode "404"로 준다."""
    if status_code == 404:
        return True
    if status_code != 400:
        return False
    try:
        payload = json.loads(body[:_ERROR_BODY_LIMIT])
    except ValueError:
        return False
    return isinstance(payload, dict) and str(payload.get("statusCode")) == "404"


async def download_object(
    object_key: str,
    dest: Path,
    *,
    max_bytes: int,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """`STORAGE_BUCKET`의 `object_key` 파일을 `dest`에 쓰고 바이트 수를 돌려준다.

    `max_bytes`를 넘으면 받는 도중 멈추고 `StorageObjectTooLarge`. `transport`는 테스트용.
    """
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_secret_key:
        raise _not_configured()
    url = (
        f"{settings.supabase_url.rstrip('/')}/storage/v1/object/"
        f"{quote(STORAGE_BUCKET, safe='')}/{quote(object_key, safe='/')}"
    )
    headers = {"apikey": settings.supabase_secret_key}
    try:
        async with (
            httpx.AsyncClient(
                timeout=TIMEOUT_SECONDS, transport=transport, follow_redirects=False
            ) as client,
            client.stream("GET", url, headers=headers) as resp,
        ):
            if resp.status_code != 200:
                body = await resp.aread()
                if _is_not_found(resp.status_code, body):
                    raise StorageObjectNotFound(f"Storage에 파일이 없습니다: {object_key}")
                raise _unavailable(retryable=resp.status_code >= 500)
            declared = resp.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise StorageObjectTooLarge(f"파일이 {max_bytes}바이트를 넘습니다")
            size = 0
            with dest.open("wb") as out:
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise StorageObjectTooLarge(f"파일이 {max_bytes}바이트를 넘습니다")
                    out.write(chunk)
            return size
    except httpx.HTTPError:
        # 예외 연결을 끊는다(요청 객체·헤더가 추적 정보에 남지 않게)
        raise _unavailable(retryable=True) from None


# Supabase 서명 업로드 URL은 2시간 고정(서버가 정함, 요청으로 바꿀 수 없다)
UPLOAD_URL_EXPIRES_SECONDS = 7200


async def create_signed_upload_url(
    object_key: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> str:
    """`STORAGE_BUCKET`의 `object_key`에 파일을 한 번 올릴 수 있는 서명 URL을 만든다.

    브라우저는 키 없이 이 URL에 파일 본문과 Content-Type을 PUT 한다(같은 URL로 다시 올리면 409).
    호출하는 쪽이 먼저 사건 권한을 확인하고 객체 키를 만든다(서버 키는 RLS를 건너뛴다).
    `transport`는 테스트용.
    """
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_secret_key:
        raise _not_configured()
    base = settings.supabase_url.rstrip("/")
    url = (
        f"{base}/storage/v1/object/upload/sign/"
        f"{quote(STORAGE_BUCKET, safe='')}/{quote(object_key, safe='/')}"
    )
    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS, transport=transport, follow_redirects=False
        ) as client:
            resp = await client.post(url, headers={"apikey": settings.supabase_secret_key})
    except httpx.HTTPError:
        raise _unavailable(retryable=True) from None
    if resp.status_code != 200:
        raise _unavailable(retryable=resp.status_code >= 500)
    try:
        signed = resp.json()["url"]
    except (ValueError, KeyError, TypeError):
        raise _unavailable(retryable=False) from None
    if not isinstance(signed, str) or not signed.startswith("/"):
        raise _unavailable(retryable=False)
    return f"{base}/storage/v1{signed}"


DOWNLOAD_URL_EXPIRES_SECONDS = 3600


async def create_signed_download_url(
    object_key: str,
    expires_in: int = DOWNLOAD_URL_EXPIRES_SECONDS,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str | None:
    """`STORAGE_BUCKET`의 `object_key`를 키 없이 내려받는 서명 URL. 파일이 없으면 None.

    영상 분석 접수가 Modal에 넘긴다(Modal에는 서버 키를 두지 않는다).
    만들면서 업로드 여부도 확인된다.
    호출하는 쪽이 먼저 사건 권한과 경로를 확인한다. `transport`는 테스트용.
    """
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_secret_key:
        raise _not_configured()
    base = settings.supabase_url.rstrip("/")
    url = (
        f"{base}/storage/v1/object/sign/"
        f"{quote(STORAGE_BUCKET, safe='')}/{quote(object_key, safe='/')}"
    )
    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS, transport=transport, follow_redirects=False
        ) as client:
            resp = await client.post(
                url,
                headers={"apikey": settings.supabase_secret_key},
                json={"expiresIn": expires_in},
            )
    except httpx.HTTPError:
        raise _unavailable(retryable=True) from None
    if _is_not_found(resp.status_code, resp.content):
        return None
    if resp.status_code != 200:
        raise _unavailable(retryable=resp.status_code >= 500)
    try:
        signed = resp.json()["signedURL"]
    except (ValueError, KeyError, TypeError):
        raise _unavailable(retryable=False) from None
    if not isinstance(signed, str) or not signed.startswith("/"):
        raise _unavailable(retryable=False)
    return f"{base}/storage/v1{signed}"
