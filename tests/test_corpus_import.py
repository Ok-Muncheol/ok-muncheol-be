"""B(통합): 운영 코퍼스 사본 받기(app/corpus_import.py).

네트워크 대신 httpx.MockTransport를 쓴다.
"""

import dataclasses
import gzip
import hashlib

import httpx
import pytest

from app import corpus_import
from app.config import get_settings
from app.retrieval.embedding import RetrievalNotConfigured
from app.retrieval.evidence_store import LocalCorpusEvidenceStore, local_evidence_store
from app.retrieval.loader import TABLES, CorpusDataError, CorpusRows

SOURCE = "corpus-index/20261113-1118"
FILES = {table: f'{{"table": "{table}"}}\n'.encode() for table in TABLES}


def settings(**over):
    base = dataclasses.replace(
        get_settings(), supabase_url="https://example.supabase.co",
        supabase_secret_key="secret-key", corpus_import_path=SOURCE,
    )
    return dataclasses.replace(base, **over)


def sums(files=FILES, broken: str | None = None) -> bytes:
    lines = []
    for table, raw in files.items():
        digest = hashlib.sha256(raw).hexdigest()
        lines.append(f"{'0' * 64 if table == broken else digest} *db_import/{table}.jsonl")
    lines.append(f"{'1' * 64} *fault_standards/charts.jsonl")  # 다른 파일 줄은 무시한다
    return ("\n".join(lines) + "\n").encode()


def storage(listing: bytes | None = None, missing: str | None = None):
    """Storage 대역. 받은 요청을 기록한다."""
    seen: list[httpx.Request] = []
    prefix = f"/storage/v1/object/{SOURCE}"

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if request.headers.get("apikey") != "secret-key":
            return httpx.Response(401)
        if path == f"{prefix}/SHA256SUMS.txt":
            return httpx.Response(200, content=listing if listing is not None else sums())
        for table, raw in FILES.items():
            if path == f"{prefix}/db_import/{table}.jsonl.gz" and table != missing:
                return httpx.Response(200, content=gzip.compress(raw))
        return httpx.Response(404)

    return httpx.MockTransport(handle), seen


def test_downloads_verifies_and_reuses_the_copy(tmp_path):
    transport, seen = storage()
    target = corpus_import.fetch_db_import(settings(), root=tmp_path, transport=transport)
    assert {p.name for p in target.iterdir()} == {*(f"{t}.jsonl" for t in TABLES), ".verified"}
    assert all((target / f"{t}.jsonl").read_bytes() == FILES[t] for t in TABLES)
    assert len(seen) == 1 + len(TABLES)
    # 같은 인스턴스에서는 검증 표시를 보고 다시 받지 않는다
    assert corpus_import.fetch_db_import(settings(), root=tmp_path, transport=transport) == target
    assert len(seen) == 1 + len(TABLES)


@pytest.mark.parametrize("case", ["hash", "missing_file", "missing_line", "auth"])
def test_any_problem_fails_closed_and_leaves_nothing(tmp_path, case):
    listing = {"hash": sums(broken="corpus_records"),
               "missing_line": b"".join(line + b"\n" for line in sums().splitlines()
                                        if b"corpus_chunks" not in line)}.get(case)
    transport, _ = storage(listing=listing,
                           missing="corpus_chunks" if case == "missing_file" else None)
    conf = settings(supabase_secret_key="wrong") if case == "auth" else settings()
    with pytest.raises(CorpusDataError) as err:
        corpus_import.fetch_db_import(conf, root=tmp_path, transport=transport)
    assert "secret" not in str(err.value) and "wrong" not in str(err.value)
    assert not list(tmp_path.rglob(".verified"))
    assert not list(tmp_path.rglob("*.jsonl"))


def test_network_error_is_corpus_data_error(tmp_path):
    def down(request):
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(CorpusDataError, match="ConnectError"):
        corpus_import.fetch_db_import(settings(), root=tmp_path,
                                      transport=httpx.MockTransport(down))


@pytest.mark.parametrize("path", ["corpus-index", "/corpus-index/", ""])
def test_path_needs_bucket_and_prefix(tmp_path, path):
    with pytest.raises(CorpusDataError, match="버킷"):
        corpus_import.fetch_db_import(settings(corpus_import_path=path), root=tmp_path)


def test_missing_storage_keys_are_not_configured(tmp_path):
    with pytest.raises(RetrievalNotConfigured, match="SUPABASE"):
        corpus_import.fetch_db_import(settings(supabase_secret_key=None), root=tmp_path)


def test_evidence_store_on_vercel_only_when_the_verified_copy_allows_it():
    corpus = CorpusRows(documents={}, chunks=[], records=[], pages=[])
    vercel = {"APP_ENV": "local", "VERCEL": "1"}
    assert local_evidence_store(corpus, vercel) is None
    assert isinstance(local_evidence_store(corpus, vercel, allow_in_production=True),
                      LocalCorpusEvidenceStore)
