"""평가 질문의 벡터 기준선 점검. 운영 검색/필터를 구현하거나 미검수 정답을 채점하지 않는다."""

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from embed_corpora import DIMENSIONS, LOCATIONS, MODEL, VERSION, key, read  # noqa: E402 -- standalone CLI adds repository root before app imports

from app.ingestion.embed import embed_chunks, validate_vector  # noqa: E402 -- standalone CLI adds repository root before app imports


async def main():
    load_dotenv(ROOT / ".env", override=False)
    for folder, version in LOCATIONS:
        base = ROOT / "data/private" / folder
        examples = read(base / "evaluation.jsonl")
        rows = read(base / version / "embeddings.jsonl")
        current = read(base / version / "chunks.jsonl")
        if len(rows) != len(current):
            raise ValueError("청크/임베딩 개수 불일치")
        for r, c in zip(rows, current, strict=False):
            if r["chunk"] != c or r["embedding_content_hash"] != key(c["text"]):
                raise ValueError("변경된 청크를 먼저 재임베딩하세요.")
            if r["embedding_model"] != MODEL or r["embedding_dimensions"] != DIMENSIONS:
                raise ValueError("모델/차원 불일치")
            validate_vector(r["embedding"], DIMENSIONS)
        cache_file = base / "evaluation_query_embeddings.jsonl"
        cached = read(cache_file) if cache_file.exists() else []
        cache = {r["text_hash"]: r for r in cached}
        pending = {
            key(e["request"]["text"]): e["request"]["text"]
            for e in examples
            if key(e["request"]["text"]) not in cache
        }
        if pending:
            queries = [{"text": text} for text in pending.values()]
            result = await embed_chunks(queries, MODEL, DIMENSIONS, VERSION)
            for (digest, text), r in zip(pending.items(), result, strict=False):
                cache[digest] = {
                    "text_hash": digest,
                    "text": text,
                    "embedding": r["embedding"],
                    "model": MODEL,
                    "dimensions": DIMENSIONS,
                }
            temp = cache_file.with_suffix(".jsonl.tmp")
            temp.write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in cache.values()),
                encoding="utf-8",
            )
            temp.replace(cache_file)
        matrix = np.asarray([r["embedding"] for r in rows], dtype=np.float32)
        matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
        results = []
        for e in examples:
            q = cache[key(e["request"]["text"])]
            if q["model"] != MODEL or q["dimensions"] != DIMENSIONS:
                raise ValueError("질의 캐시 모델/차원 불일치")
            validate_vector(q["embedding"], DIMENSIONS)
            vector = np.asarray(q["embedding"], dtype=np.float32)
            scores = matrix @ (vector / np.linalg.norm(vector))
            order = np.argsort(-scores, kind="stable")
            unique = []
            seen = set()
            for index in order:
                chunk = rows[int(index)]["chunk"]
                if chunk["parent_id"] in seen:
                    continue
                seen.add(chunk["parent_id"])
                unique.append(
                    {
                        "parent_id": chunk["parent_id"],
                        "chunk_id": chunk["chunk_id"],
                        "cosine_similarity": float(scores[index]),
                        "page_start": chunk["page_start"],
                        "page_end": chunk["page_end"],
                        "excerpt": chunk["text"][:650],
                    }
                )
            positions = {r["parent_id"]: i + 1 for i, r in enumerate(unique)}
            expected = e["expected_parent_ids"]
            results.append(
                {
                    "example_id": e["example_id"],
                    "query": e["request"]["text"],
                    "label_status": e["label_status"],
                    "candidate_gold_parent_ids": expected,
                    "candidate_gold_ranks": {p: positions.get(p) for p in expected},
                    "top5": unique[:5],
                    "needs_source_review": any(positions.get(p, 99999) > 3 for p in expected),
                }
            )
        report = {
            "status": "diagnostic_only_not_scored",
            "model": MODEL,
            "dimensions": DIMENSIONS,
            "evaluation_dataset_hash": hashlib.sha256(
                (base / "evaluation.jsonl").read_bytes()
            ).hexdigest(),
            "strategy": "cosine_chunk_scores_max_per_parent",
            "query_count": len(results),
            "chunk_count": len(rows),
            "gold_outside_top3_count": sum(r["needs_source_review"] for r in results),
            "limitations": [
                "미검수 정답 후보의 순위 점검. 확정 성능 지표 아님",
                "날짜·적용판·속성 필터 미적용",
                "BM25·재순위 미적용",
                "②는 사실과 판단 근거 청크 전체를 대상으로 함",
            ],
            "results": results,
        }
        target = base / "evaluation_results.json"
        temp = target.with_suffix(".json.tmp")
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(target)
        print(
            folder,
            "queries",
            len(results),
            "candidate gold outside top3",
            report["gold_outside_top3_count"],
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(main())
