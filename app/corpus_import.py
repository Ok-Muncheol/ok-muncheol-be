"""B(통합): 운영 서버가 비공개 Storage에서 코퍼스 db_import 사본을 받아 검증한다.

로컬은 `data/private/corpus-db-import/db_import`를 그대로 읽는다(D `app/retrieval/loader.py`).
운영(Vercel)은 그 폴더가 git 밖이라 배포에 없다. `CORPUS_IMPORT_PATH`(`<버킷>/<접두어>`, 예
`corpus-index/20261113-1118`)가 있으면 Supabase Storage 비공개 버킷에서 다음을 받는다.

- `<접두어>/SHA256SUMS.txt`: C가 보낸 적재 파일 묶음의 해시 목록 그대로
- `<접두어>/db_import/<표>.jsonl.gz`: D 로더가 읽는 네 표(`loader.TABLES`)의 gzip 압축본

압축을 푼 파일 해시가 SHA256SUMS.txt와 모두 같을 때만 그 폴더를 돌려준다. 못 받거나 하나라도
다르면 `CorpusDataError`라 지식베이스를 켜지 않는다(fail-closed). 인스턴스 임시 폴더에 검증 표시를
남겨 같은 인스턴스에서는 다시 받지 않는다. 버킷은 공개하지 않는다(원문·청크·벡터, 공개 금지).
"""

import gzip
import hashlib
import shutil
import tempfile
import zlib
from pathlib import Path

import httpx

from app.config import Settings
from app.retrieval.embedding import RetrievalNotConfigured
from app.retrieval.loader import TABLES, CorpusDataError

DOWNLOAD_TIMEOUT_SECONDS = 60.0
MARKER = ".verified"


def import_root() -> Path:
    """받은 사본을 두는 곳. Vercel 함수는 /tmp에만 쓸 수 있다."""
    return Path(tempfile.gettempdir()) / "corpus-db-import"


def _source(settings: Settings) -> tuple[str, str]:
    source = (settings.corpus_import_path or "").strip().strip("/")
    bucket, _, prefix = source.partition("/")
    prefix = prefix.strip("/")
    if not bucket or not prefix:
        raise CorpusDataError(
            f"B: CORPUS_IMPORT_PATH는 '<버킷>/<접두어>' 모양이어야 합니다: {source!r}"
        )
    return bucket, prefix


def _expected_sums(text: str) -> dict[str, str]:
    """`<해시> *<경로>` 줄 목록 → {경로: 해시}."""
    sums: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            sums[parts[1].strip().lstrip("*")] = parts[0].lower()
    return sums


def _get(client: httpx.Client, url: str, headers: dict[str, str], name: str) -> bytes:
    resp = client.get(url, headers=headers)
    if resp.status_code != 200:
        raise CorpusDataError(f"B: 코퍼스 사본 {name}을 받지 못했습니다(HTTP {resp.status_code})")
    return resp.content


def fetch_db_import(
    settings: Settings, *, root: Path | None = None, transport: httpx.BaseTransport | None = None
) -> Path:
    """`CORPUS_IMPORT_PATH`의 db_import를 받아 검증한 폴더. D `read_corpus`에 그대로 넘긴다."""
    bucket, prefix = _source(settings)
    if not settings.supabase_url or not settings.supabase_secret_key:
        raise RetrievalNotConfigured(
            "B: 코퍼스 사본을 받을 SUPABASE_URL·SUPABASE_SECRET_KEY가 없습니다"
        )
    digest = hashlib.sha256(f"{bucket}/{prefix}".encode()).hexdigest()[:16]
    folder = (root or import_root()) / digest
    target = folder / "db_import"
    if (target / MARKER).is_file():
        return target
    staging = folder / "db_import.part"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    base = f"{settings.supabase_url.rstrip('/')}/storage/v1/object/{bucket}/{prefix}"
    headers = {"apikey": settings.supabase_secret_key}
    try:
        with httpx.Client(timeout=DOWNLOAD_TIMEOUT_SECONDS, transport=transport) as client:
            listing = _get(client, f"{base}/SHA256SUMS.txt", headers, "SHA256SUMS.txt")
            sums = _expected_sums(listing.decode("utf-8"))
            for table in TABLES:
                name = f"db_import/{table}.jsonl"
                if name not in sums:
                    raise CorpusDataError(f"B: SHA256SUMS.txt에 {name}이 없습니다")
                raw = gzip.decompress(_get(client, f"{base}/{name}.gz", headers, f"{name}.gz"))
                if hashlib.sha256(raw).hexdigest() != sums[name]:
                    raise CorpusDataError(f"B: 받은 {name}의 SHA-256이 SHA256SUMS.txt와 다릅니다")
                (staging / f"{table}.jsonl").write_bytes(raw)
    except CorpusDataError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except (httpx.HTTPError, OSError, EOFError, zlib.error, UnicodeDecodeError) as exc:
        # gzip.BadGzipFile은 OSError다. 메시지에 주소·키를 넣지 않는다
        shutil.rmtree(staging, ignore_errors=True)
        raise CorpusDataError(f"B: 코퍼스 사본을 받지 못했습니다: {type(exc).__name__}") from exc
    (staging / MARKER).write_text(f"{bucket}/{prefix}\n", encoding="utf-8")
    shutil.rmtree(target, ignore_errors=True)
    staging.rename(target)
    return target
