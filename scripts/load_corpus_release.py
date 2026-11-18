"""B(통합): C가 보낸 코퍼스 적재 파일 묶음을 DB 색인(pgvector)에 한 판으로 넣는다. 사람이 실행한다.

    .venv/bin/python scripts/load_corpus_release.py <묶음 폴더> [--release 이름] [--note 메모]
    .venv/bin/python scripts/load_corpus_release.py --verify <묶음 폴더> [--release 이름]
    .venv/bin/python scripts/load_corpus_release.py --list
    .venv/bin/python scripts/load_corpus_release.py --delete <이름>

- 묶음 폴더는 `SHA256SUMS.txt`와 `db_import/` 네 파일이 있는 곳이다(zip을 푼 폴더). 먼저 파일을
  SHA256SUMS.txt와 대조하고, 하나라도 다르면 아무것도 넣지 않는다.
- 판 이름은 `--release`, 없으면 폴더 이름에서 `corpus-db-import-`를 뗀 값이다(예: 20261116-1742).
  같은 이름의 판이 이미 있으면 덮어쓰지 않고 멈춘다. 서버가 읽는 판은 고치지 않는다.
  새 자료는 새 이름으로 넣고 `CORPUS_DB_RELEASE`를 바꾼다.
- 한 판은 한 트랜잭션으로 넣는다. 넣은 뒤 서버가 쓰는 읽기 코드로 다시 읽어 파일과 대조한다
  (청크·레코드·페이지·벡터·낱말 수). `--verify`는 넣지 않고 대조만 한다.
- 키워드 검색용 낱말 수는 simple 토큰화로 늘 만들고, kiwipiepy가 깔려 있으면 kiwi로도 만든다
  (서버는 Kiwi가 있으면 kiwi, 없으면 simple로 질의를 나눈다). `--tokenizer`로 직접 고를 수 있다.
- DATABASE_URL은 환경 변수, 없으면 .env에서 읽는다(Session pooler 주소).
  주소·비밀번호는 출력하지 않는다.
- 원문·청크·벡터가 들어가는 표라 브라우저 권한이 없다(마이그레이션 20261118180000).
  공개 DB에 넣지 않는다.
"""

import argparse
import asyncio
import hashlib
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PREFIX = "corpus-db-import-"


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url and (ROOT / ".env").is_file():
        from dotenv import dotenv_values

        url = dotenv_values(ROOT / ".env").get("DATABASE_URL") or ""
    if not url:
        sys.exit("DATABASE_URL이 없다(.env 또는 환경 변수)")
    return url


def _target(url: str) -> str:
    """어느 DB에 쓰는지 한 낱말로(주소는 출력하지 않는다)."""
    host = url.rpartition("@")[2].split("/", 1)[0].rsplit(":", 1)[0]
    if host in ("127.0.0.1", "localhost", "::1"):
        return "로컬 Postgres"
    return "Supabase" if host.endswith((".supabase.co", ".supabase.com")) else "원격 Postgres"


def read_sums(folder: Path, tables: tuple[str, ...]) -> dict[str, str]:
    """db_import 네 파일을 SHA256SUMS.txt와 대조하고 {표 이름: 해시}를 돌려준다. 다르면 멈춘다."""
    listing = folder / "SHA256SUMS.txt"
    if not listing.is_file():
        sys.exit(f"SHA256SUMS.txt가 없다: {folder}")
    listed: dict[str, str] = {}
    for line in listing.read_text(encoding="utf-8").splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            listed[parts[1].strip().lstrip("*")] = parts[0].lower()
    sums: dict[str, str] = {}
    for table in tables:
        name = f"db_import/{table}.jsonl"
        path = folder / name
        if not path.is_file():
            sys.exit(f"파일이 없다: {name}")
        if name not in listed:
            sys.exit(f"SHA256SUMS.txt에 {name}이 없다")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != listed[name]:
            sys.exit(f"{name}의 SHA-256이 SHA256SUMS.txt와 다르다 — 아무것도 넣지 않았다")
        sums[table] = digest
    return sums


def _tokenizers(names: list[str] | None) -> list:
    from app.retrieval.keyword import KiwiTokenizer, SimpleTokenizer

    known = {"simple": SimpleTokenizer, "kiwi": KiwiTokenizer}
    if names is None:
        tokenizers = [SimpleTokenizer()]
        try:
            tokenizers.append(KiwiTokenizer())
        except ImportError:
            pass
        return tokenizers
    unknown = sorted(set(names) - set(known))
    if unknown:
        sys.exit(f"모르는 토큰화: {unknown} (simple·kiwi)")
    try:
        return [known[name]() for name in dict.fromkeys(names)]
    except ImportError:
        sys.exit('kiwipiepy가 없다 — pip install -e ".[keyword]"')


def _release_name(folder: Path, given: str | None) -> str:
    if given:
        return given
    if folder.name.startswith(PREFIX) and len(folder.name) > len(PREFIX):
        return folder.name.removeprefix(PREFIX)
    sys.exit(f"판 이름을 --release로 준다(폴더 이름이 {PREFIX}<이름> 모양이 아니다)")


def _connect(url: str):
    import psycopg

    from app.db import normalize_database_url

    return psycopg.connect(normalize_database_url(url), connect_timeout=15)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="코퍼스 적재 파일 묶음 → DB 색인의 한 판")
    add = parser.add_argument
    add("folder", nargs="?", type=Path, help="SHA256SUMS.txt와 db_import/가 있는 폴더")
    add("--release", help="판 이름(소문자·숫자·점·밑줄·붙임표)")
    add("--note", help="판에 남길 메모(예: 6차 검수 97/108)")
    add("--tokenizer", action="append", help="낱말 수를 만들 토큰화(여러 번 줄 수 있다)")
    add("--skip-verify", action="store_true", help="넣은 뒤 다시 읽어 대조하지 않는다")
    add("--verify", action="store_true", help="넣지 않고 DB의 판을 파일과 대조만 한다")
    add("--list", action="store_true", help="DB에 있는 판 목록")
    add("--delete", metavar="이름", help="판 하나를 지운다(그 판의 모든 행)")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    import psycopg

    from app.retrieval.embedding import RetrievalNotConfigured
    from app.retrieval.loader import TABLES, read_corpus
    from app.retrieval.pg_load import delete_release, list_releases, load_release, verify_release

    url = _database_url()
    print(f"대상 DB: {_target(url)}")
    if args.list:
        with _connect(url) as conn:
            releases = list_releases(conn)
        for row in releases:
            print(f"{row['release']}  {row['loaded_at'].astimezone():%Y-%m-%d %H:%M}  "
                  f"청크 {row['row_counts'].get('corpus_chunks')}  "
                  f"토큰화 {','.join(row['keyword_tokenizers'])}  {row['note'] or ''}")
        print(f"판 {len(releases)}개")
        return
    if args.delete:
        with _connect(url) as conn:
            deleted = delete_release(conn, args.delete)
            conn.commit()
        print(f"{'지운 판' if deleted else '없는 판'}: {args.delete}")
        return
    if args.folder is None:
        parser.error("묶음 폴더를 준다")
    folder = args.folder.resolve()
    name = _release_name(folder, args.release)
    sums = read_sums(folder, TABLES)
    print(f"파일 확인: 표 {len(sums)}개 해시가 SHA256SUMS.txt와 같다")
    try:
        corpus = read_corpus(folder / "db_import")
    except RetrievalNotConfigured as exc:
        sys.exit(f"적재 파일을 읽지 못했다: {exc}")
    tokenizers = _tokenizers(args.tokenizer)
    names = [tokenizer.name for tokenizer in tokenizers]
    if not args.verify:
        started = time.perf_counter()
        try:
            with _connect(url) as conn:
                conn.execute("set statement_timeout = '15min'")
                counts = load_release(
                    conn, name, corpus, source_sha256=sums, tokenizers=tokenizers, note=args.note
                )
                conn.commit()
        except psycopg.errors.UniqueViolation:
            sys.exit(
                f"이미 있는 판이다: {name} — 덮어쓰지 않는다."
                " 다른 이름으로 넣거나 --delete 뒤 다시 넣는다"
            )
        except (ValueError, RetrievalNotConfigured) as exc:
            sys.exit(f"넣지 못했다(아무것도 남지 않았다): {exc}")
        seconds = time.perf_counter() - started
        print(f"넣은 판: {name} ({seconds:.0f}초) {counts}, 토큰화 {names}")
    if args.skip_verify:
        return
    started = time.perf_counter()
    try:
        checked = asyncio.run(
            verify_release(url, name, corpus, source_sha256=sums, tokenizers=tokenizers)
        )
    except RetrievalNotConfigured as exc:
        sys.exit(f"대조 실패: {exc}")
    print(f"다시 읽어 대조 통과({time.perf_counter() - started:.0f}초): {checked}")
    print(f"서버가 이 판을 읽게 하려면 환경 변수를 CORPUS_DB_RELEASE={name} 으로 둔다")


if __name__ == "__main__":
    main()
