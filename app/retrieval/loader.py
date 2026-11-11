"""D: C의 코퍼스 DB 적재 파일(db_import/*.jsonl)을 읽어 InMemoryIndex와 로컬 EvidenceStore를 만든다.

Supabase(pgvector)로 옮기기 전 로컬 확인용이다. 파일은 C가 `scripts/export_corpus_index_rows.py`로
만들어 비공개로 전달한다(저장소에 올리지 않음). 테이블·칼럼 뜻은 `docs/contracts/corpus-index.md`.

    corpus = read_corpus()                          # 파일이 없으면 RetrievalNotConfigured
    configure_retrieval(
        index=build_index(corpus, keyword_corpora=keyword_corpora_for(options)),
        embedder=OpenAIEmbedder.from_env(),
        options=options,
        default_terms_edition=corpus.terms_edition(),
    )

검색 결과의 청크와 EvidenceStore.get_chunk는 같은 조인(`search_chunk`)으로 만든다.
C의 validate_hits가 두 값을 필드별로 비교하기 때문이다.

파일·데이터 문제는 모두 RetrievalNotConfigured다. 파일이 없으면 그 클래스, 있는데 쓸 수 없으면
(읽기 오류, UTF-8 아님, 빈 파일, 깨진 JSON, 칼럼·타입 오류, 끊긴 연결, 중복, 벡터 길이,
handoff.json)
하위 클래스 CorpusDataError다. 메시지에 파일·줄·문제를 담고 원인 예외는 __cause__로 남긴다.
행 모양을 먼저 검사하므로 데이터 때문에 KeyError·TypeError가 나지 않는다. 그 밖의 예외는 코드
문제로 보고 감싸지 않는다.
"""

import copy
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import get_args

from app.retrieval.embedding import EmbeddingSpec, RetrievalNotConfigured
from app.retrieval.index import InMemoryIndex
from app.retrieval.keyword import Tokenizer
from app.schemas.retrieval import Corpus, EmbeddedChunk, SearchChunk, SourceDocument

ROOT = Path(__file__).resolve().parents[2]
DB_IMPORT = ROOT / "data/private/corpus-db-import/db_import"
TABLES = ("corpus_documents", "corpus_chunks", "corpus_records", "corpus_source_pages")
# SourceDocument 9개 필드. corpus_documents의 나머지 칼럼(source_name·usage_review 등)은 넣지 않는다
DOCUMENT_FIELDS: tuple[str, ...] = tuple(SourceDocument.__annotations__)
# 선택 필드: 이전 적재 파일에 칼럼이 없으면 None으로 채운다(키는 늘 둔다)
OPTIONAL_DOCUMENT_FIELDS: frozenset[str] = frozenset(SourceDocument.__optional_keys__)
CORPORA: frozenset[str] = frozenset(get_args(Corpus))


class CorpusDataError(RetrievalNotConfigured):
    """코퍼스 파일이 있지만 쓸 수 없다.

    RetrievalNotConfigured 하위라 연결 실패 처리(except RetrievalNotConfigured)에 함께 걸린다.
    """


def _fail(path: Path, problem: str, line: int | None = None) -> CorpusDataError:
    where = path.name if line is None else f"{path.name} {line}번째 줄"
    return CorpusDataError(f"D: 코퍼스 파일 {where}: {problem} ({path})")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_date(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def read_handoff(root: Path = ROOT) -> dict:
    """data/private/handoff.json.

    없으면 RetrievalNotConfigured, 못 읽거나 깨졌으면 CorpusDataError.
    """
    path = root / "data/private/handoff.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RetrievalNotConfigured(f"D: 파일이 없습니다: {path}") from exc
    except OSError as exc:
        raise _fail(path, f"읽을 수 없습니다({type(exc).__name__})") from exc
    except ValueError as exc:  # UnicodeDecodeError, JSONDecodeError
        raise _fail(path, f"JSON으로 읽을 수 없습니다({type(exc).__name__})") from exc
    if not isinstance(data, dict):
        raise _fail(path, "최상위가 JSON 객체가 아닙니다")
    return data


def handoff_embedding_spec(root: Path = ROOT) -> EmbeddingSpec:
    """질의 임베더 기대값. handoff.json은 저장소에 있어 코퍼스 파일이 없는 운영에서도 읽힌다."""
    embedding = read_handoff(root).get("embedding")
    if not (
        isinstance(embedding, dict)
        and isinstance(embedding.get("model"), str)
        and _is_int(embedding.get("dimensions"))
        and isinstance(embedding.get("version"), str)
    ):
        raise _fail(
            root / "data/private/handoff.json",
            "embedding.model·dimensions·version이 없거나 타입이 틀렸습니다",
        )
    return EmbeddingSpec(embedding["model"], embedding["dimensions"], embedding["version"])


def source_document(row: dict) -> SourceDocument:
    """corpus_documents 행 → SourceDocument(9개 필드). 선택 필드는 칼럼이 없으면 None."""
    document = {
        name: row.get(name) if name in OPTIONAL_DOCUMENT_FIELDS else row[name]
        for name in DOCUMENT_FIELDS
    }
    month = document.get("published_month")
    if month is not None and not _MONTH.fullmatch(str(month)):
        name = f"corpus_documents.jsonl {row['document_id']}"
        raise CorpusDataError(f"{name}: published_month {month!r}는 YYYY-MM이 아니다")
    return document  # type: ignore[return-value]


def search_chunk(row: dict, documents: dict[str, dict]) -> SearchChunk:
    """corpus_chunks 행 + corpus_documents 조인 → SearchChunk.

    raw_text·display_text·임베딩 칼럼은 뺀다.
    """
    return {
        "chunk_id": row["chunk_id"],
        "parent_id": row["parent_id"],
        "document": source_document(documents[row["document_id"]]),
        "page_start": row["page_start"],
        "page_end": row["page_end"],
        "text": row["text"],
        "metadata": copy.deepcopy(row["metadata"]),  # 색인·어댑터가 같은 사전을 나눠 쓰지 않게
    }


_INT = "int"
_OPT_STR = (str, type(None))
_MONTH = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
# 표마다 반드시 있어야 하는 칼럼과 타입. 나머지 칼럼은 보지 않는다(C가 칼럼을 늘려도 깨지지 않게)
_COLUMNS: dict[str, dict[str, object]] = {
    "corpus_documents": {
        "document_id": str,
        "corpus": str,
        "source_uri": str,
        "edition": str,
        "effective_from": str,
        "effective_to": _OPT_STR,
        "content_hash": str,
        "parser_version": str,
    },
    "corpus_chunks": {
        "corpus": str,
        "document_id": str,
        "chunk_id": str,
        "parent_id": str,
        "page_start": _INT,
        "page_end": _INT,
        "text": str,
        "metadata": dict,
        "embedding_model": str,
        "embedding_dimensions": _INT,
        "embedding_version": str,
        "embedding": list,
    },
    "corpus_records": {
        "corpus": str,
        "kind": str,
        "record_id": str,
        "parent_id": _OPT_STR,
        "is_parent": bool,
        "body": dict,
    },
    "corpus_source_pages": {"document_id": str, "page": _INT, "content_hash": str},
}


def _row_problem(table: str, row: object) -> str | None:
    """행 모양 문제(없으면 None). 표 사이 연결과 중복은 read_corpus가 본다."""
    if not isinstance(row, dict):
        return "행이 JSON 객체가 아닙니다"
    for name, kind in _COLUMNS[table].items():
        if name not in row:
            return f"칼럼 {name}이 없습니다"
        value = row[name]
        ok = _is_int(value) if kind == _INT else isinstance(value, kind)  # type: ignore[arg-type]
        if not ok:
            return f"칼럼 {name}의 타입이 틀렸습니다({type(value).__name__})"
    if "corpus" in row and row["corpus"] not in CORPORA:
        return f"모르는 코퍼스입니다: {row['corpus']}"
    if table == "corpus_documents":
        for name in ("effective_from", "effective_to"):
            if row[name] and not _is_date(row[name]):
                return f"{name}가 날짜(YYYY-MM-DD)가 아닙니다: {row[name]}"
    elif table == "corpus_chunks":
        chunk_id, vector = row["chunk_id"], row["embedding"]
        dimensions = row["embedding_dimensions"]
        if len(vector) != dimensions:
            return f"청크 {chunk_id}: 벡터 길이 {len(vector)} ≠ 차원 {dimensions}"
        if not all(_is_number(x) for x in vector):
            return f"청크 {chunk_id}: 벡터에 숫자가 아닌 값이 있습니다"
        for name in ("accident_date_from", "accident_date_to"):
            value = row["metadata"].get(name)
            if value and not _is_date(value):
                return f"청크 {chunk_id}: metadata.{name}가 날짜가 아닙니다: {value}"
    elif table == "corpus_records" and row["is_parent"] and row["parent_id"] is None:
        return f"부모 레코드 {row['record_id']}에 parent_id가 없습니다"
    return None


def _read_jsonl(path: Path, table: str) -> list[dict]:
    """파일 → 행 목록.

    없으면 RetrievalNotConfigured, 읽기·JSON·빈 파일·행 모양 문제는 CorpusDataError.
    """
    if not path.is_file():
        raise RetrievalNotConfigured(f"D: 코퍼스 적재 파일이 없습니다: {path}")
    rows: list[dict] = []
    try:
        with path.open(encoding="utf-8") as lines:
            for number, line in enumerate(lines, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise _fail(path, f"JSON이 아닙니다({exc.msg})", number) from exc
                problem = _row_problem(table, row)
                if problem:
                    raise _fail(path, problem, number)
                rows.append(row)
    except OSError as exc:
        raise _fail(path, f"읽을 수 없습니다({type(exc).__name__})") from exc
    except UnicodeDecodeError as exc:
        raise _fail(path, "UTF-8로 읽을 수 없습니다") from exc
    if not rows:
        raise _fail(path, "행이 없습니다(빈 파일)")
    return rows


@dataclass(frozen=True)
class CorpusRows:
    """db_import 네 파일의 행. 읽을 때 행 모양과 조인에 필요한 연결을 검사한다."""

    documents: dict[str, dict]  # document_id → corpus_documents 행
    chunks: list[dict]  # corpus_chunks 행(embedding 포함)
    records: list[dict]  # corpus_records 행
    pages: list[dict]  # corpus_source_pages 행

    def terms_edition(self) -> str:
        """약관 판. 판이 하나가 아니면 고를 수 없어 CorpusDataError."""
        editions = {d["edition"] for d in self.documents.values() if d["corpus"] == "policy_terms"}
        if len(editions) != 1:
            raise CorpusDataError(
                f"D: corpus_documents.jsonl: 약관 판을 하나로 정할 수 없습니다: {sorted(editions)}"
            )
        return editions.pop()


def _read_documents(path: Path) -> dict[str, dict]:
    documents: dict[str, dict] = {}
    for doc in _read_jsonl(path, "corpus_documents"):
        if doc["document_id"] in documents:
            raise _fail(path, f"같은 document_id가 두 번 있습니다: {doc['document_id']}")
        documents[doc["document_id"]] = doc
    return documents


def read_corpus(base: Path = DB_IMPORT) -> CorpusRows:
    """db_import/*.jsonl을 읽는다.

    파일이 없으면 RetrievalNotConfigured, 있는데 쓸 수 없으면 CorpusDataError(하위 클래스).
    """
    missing = [name for name in TABLES if not (base / f"{name}.jsonl").is_file()]
    if missing:
        raise RetrievalNotConfigured(
            f"D: 코퍼스 적재 파일이 없습니다({base}): {', '.join(missing)}"
        )
    documents = _read_documents(base / "corpus_documents.jsonl")
    rows = {name: _read_jsonl(base / f"{name}.jsonl", name) for name in TABLES[1:]}
    path = base / "corpus_chunks.jsonl"
    seen: set[str] = set()
    specs: set[tuple[str, int, str]] = set()
    for chunk in rows["corpus_chunks"]:
        chunk_id = chunk["chunk_id"]
        doc = documents.get(chunk["document_id"])
        if doc is None:
            raise _fail(path, f"청크 {chunk_id}의 문서가 corpus_documents에 없습니다")
        if doc["corpus"] != chunk["corpus"]:
            raise _fail(path, f"청크 {chunk_id}의 코퍼스가 문서와 다릅니다")
        if chunk_id in seen:  # 색인은 chunk_id 하나로 행을 찾는다
            raise _fail(path, f"같은 chunk_id가 두 번 있습니다: {chunk_id}")
        seen.add(chunk_id)
        specs.add(
            (chunk["embedding_model"], chunk["embedding_dimensions"], chunk["embedding_version"])
        )
    if len(specs) != 1:  # 한 색인에는 한 임베딩 모델·차원·버전만 넣는다
        raise _fail(path, f"임베딩 모델·차원·버전이 하나가 아닙니다: {sorted(specs)}")
    return CorpusRows(
        documents=documents,
        chunks=rows["corpus_chunks"],
        records=rows["corpus_records"],
        pages=rows["corpus_source_pages"],
    )


def embedded_chunks(
    corpus: CorpusRows, corpora: Iterable[Corpus] | None = None
) -> list[EmbeddedChunk]:
    wanted = None if corpora is None else set(corpora)
    return [
        {
            "chunk": search_chunk(row, corpus.documents),
            "embedding": row["embedding"],
            "embedding_model": row["embedding_model"],
            "embedding_dimensions": row["embedding_dimensions"],
            "embedding_version": row["embedding_version"],
        }
        for row in corpus.chunks
        if wanted is None or row["corpus"] in wanted
    ]


def build_index(
    corpus: CorpusRows,
    corpora: Iterable[Corpus] | None = None,
    *,
    tokenizer: Tokenizer | None = None,
    keyword_corpora: Iterable[Corpus] | None = None,
) -> InMemoryIndex:
    """읽은 행으로 색인을 만든다(기본은 세 코퍼스 전체).

    keyword_corpora: BM25 통계를 만들 코퍼스(None이면 전체). 벡터만 쓰는 코퍼스는 빼면 된다.
    색인의 적재 검사(날짜·임베딩, ValueError)에 걸리면 CorpusDataError. read_corpus가 먼저 거른다.
    """
    index = InMemoryIndex(tokenizer=tokenizer, keyword_corpora=keyword_corpora)
    try:
        index.add(embedded_chunks(corpus, corpora))
    except ValueError as exc:
        raise CorpusDataError(f"D: 코퍼스 색인 적재 실패: {exc}") from exc
    return index


def load_index(
    corpora: Iterable[Corpus] | None = None,
    *,
    base: Path = DB_IMPORT,
    tokenizer: Tokenizer | None = None,
    keyword_corpora: Iterable[Corpus] | None = None,
) -> InMemoryIndex:
    """read_corpus + build_index. 어댑터도 필요하면 read_corpus 결과를 함께 쓴다(한 번만 읽기)."""
    return build_index(
        read_corpus(base), corpora, tokenizer=tokenizer, keyword_corpora=keyword_corpora
    )


def corpus_terms_edition(base: Path = DB_IMPORT) -> str:
    """corpus_documents의 약관 판(handoff.json 대신). 문서 파일만 읽는다."""
    documents = _read_documents(base / "corpus_documents.jsonl")
    return CorpusRows(documents=documents, chunks=[], records=[], pages=[]).terms_edition()
