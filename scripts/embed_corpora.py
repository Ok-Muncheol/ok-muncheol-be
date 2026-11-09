"""현재 코퍼스에 임베딩 저장. content hash 기반 캐시로 중단 후 재개한다."""

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.ingestion.embed import embed_chunks, validate_vector  # noqa: E402 -- standalone CLI adds repository root before app imports

MODEL = "text-embedding-3-small"
DIMENSIONS = 1536
VERSION = "text-embedding-3-small:1536:v1"
LOCATIONS = [
    ("01_fault_standards", "processed"),
    ("02_dispute_cases", "processed"),
    ("03_policy_terms", "processed"),
]


def read(path):
    return [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines() if s.strip()]


def key(text):
    return hashlib.sha256(f"{VERSION}\0{text}".encode()).hexdigest()


async def main():
    import tiktoken
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", override=False)
    tokenizer = tiktoken.get_encoding("cl100k_base")
    corpora = []
    for folder, version in LOCATIONS:
        path = ROOT / "data/private" / folder / version
        chunks = read(path / "chunks.jsonl")
        if len({c["chunk_id"] for c in chunks}) != len(chunks):
            raise ValueError(f"중복 청크 ID: {folder}")
        for c in chunks:
            if (
                not c["text"].strip()
                or len(tokenizer.encode(c["text"], disallowed_special=())) > 8191
            ):
                raise ValueError(f"빈 본문 또는 토큰 한도 초과: {c['chunk_id']}")
        corpora.append((path, chunks))
    tokens = sum(
        len(tokenizer.encode(c["text"], disallowed_special=())) for _, cs in corpora for c in cs
    )
    print(
        f"model={MODEL} dimensions={DIMENSIONS} chunks={sum(len(cs) for _, cs in corpora)} input_tokens={tokens}",  # noqa: E501 -- source text or prompt kept intact
        flush=True,
    )
    if args.dry_run:
        return
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("project3/.env에 OPENAI_API_KEY가 없습니다. 키 값은 출력하지 않습니다.")
    for path, chunks in corpora:
        cache_path = path / "embedding_cache.jsonl"
        cache = {}
        if cache_path.exists():
            # 중단으로 잘린 마지막 줄만 버린다. 손상된 중간 줄은 오류로 중단한다.
            lines = cache_path.read_text(encoding="utf-8").splitlines()
            valid = []
            for i, line in enumerate(lines):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    if i != len(lines) - 1:
                        raise
                    break
                validate_vector(row["embedding"], DIMENSIONS)
                cache[row["key"]] = row["embedding"]
                valid.append(line)
            cache_path.write_text("".join(s + "\n" for s in valid), encoding="utf-8")
        pending = {key(c["text"]): c for c in chunks if key(c["text"]) not in cache}
        work = list(pending.items())
        for offset in range(0, len(work), 32):
            batch = work[offset : offset + 32]
            vectors = await embed_chunks([c for _, c in batch], MODEL, DIMENSIONS, VERSION)
            with cache_path.open("a", encoding="utf-8") as stream:
                for (digest, _), row in zip(batch, vectors, strict=False):
                    stream.write(json.dumps({"key": digest, "embedding": row["embedding"]}) + "\n")
                    cache[digest] = row["embedding"]
                stream.flush()
                os.fsync(stream.fileno())
            print(
                f"{path.parent.name}: {min(offset + 32, len(work))}/{len(work)} new vectors",
                flush=True,
            )
        temporary = path / "embeddings.jsonl.tmp"
        with temporary.open("w", encoding="utf-8") as stream:
            for c in chunks:
                row = {
                    "chunk": c,
                    "embedding": cache[key(c["text"])],
                    "embedding_model": MODEL,
                    "embedding_dimensions": DIMENSIONS,
                    "embedding_version": VERSION,
                    "embedding_content_hash": key(c["text"]),
                }
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        temporary.replace(path / "embeddings.jsonl")
        print(f"saved {path.parent.name}: {len(chunks)} embeddings", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
