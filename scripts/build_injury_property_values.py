"""D: 지금 코퍼스 부모 원문 → 대인·대물 숫자 자료(`schedule_value`) 파일.

새 코퍼스 zip(김지은 님 빌드에 연결된 판)이 나오기 전에 계산 코드를 개발하려고 만든다.
부모 원문과 대조(`app.ingestion.injury_property_values.records`)한 뒤 두 파일을 쓴다.

- `schedule_values.jsonl`: 레코드 본문(빌드 산출물 `schedule_values.jsonl`과 같은 모양)
- `corpus_records_rows.jsonl`: DB 적재 행 모양(`corpus_records`, kind=schedule_value)

결과는 data/private 아래에만 쓴다(원문 문장이 들어 있어 커밋하지 않는다).

    python scripts/build_injury_property_values.py [--records PATH] [--output DIR]
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.ingestion import injury_property_values  # noqa: E402 -- standalone CLI adds repository root before app imports

PRIVATE = ROOT / "data" / "private"
DEFAULT_RECORDS = PRIVATE / "corpus-db-import" / "db_import" / "corpus_records.jsonl"
DEFAULT_OUTPUT = PRIVATE / "injury_property_values"
LIMIT_PARENT = ":payment_standard:별표1:표1"


def read_parents(path: Path) -> tuple[dict[str, str], dict]:
    parents: dict[str, str] = {}
    doc = None
    with path.open(encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            if row.get("corpus") != "policy_terms" or not row.get("is_parent"):
                continue
            body = row.get("body") or {}
            parents[row["parent_id"]] = body.get("text") or ""
            if row["parent_id"].endswith(LIMIT_PARENT):
                doc = body["document"]
    if doc is None:
        raise SystemExit(f"약관 별표1 표1 부모가 없다: {path}")
    return parents, doc


def db_row(record: dict) -> dict:
    page = record["source_pdf_page"]
    return {
        "corpus": "policy_terms",
        "kind": "schedule_value",
        "record_id": record["record_id"],
        "parent_id": None,
        "is_parent": False,
        "document_id": record["document"]["document_id"],
        "chart_id": None,
        "page_start": page,
        "page_end": page,
        "body": record,
    }


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    a = parser.parse_args()
    output = a.output.resolve()
    if not output.is_relative_to(PRIVATE.resolve()):
        raise SystemExit(f"결과는 data/private 아래에만 쓴다: {output}")
    parents, doc = read_parents(a.records)
    records = injury_property_values.records(parents, doc)
    output.mkdir(parents=True, exist_ok=True)
    write_jsonl(output / "schedule_values.jsonl", records)
    write_jsonl(output / "corpus_records_rows.jsonl", [db_row(r) for r in records])
    summary = {
        "document_id": doc["document_id"],
        "content_hash": doc.get("content_hash"),
        "records": len(records),
        "value_types": dict(Counter(r["value_type"] for r in records)),
        "calculation_ready": sum(r["calculation_ready"] for r in records),
        "output": str(output.relative_to(ROOT)),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
