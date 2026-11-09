"""페이지 추출 JSONL에서 ① 구조 레코드와 청크를 생성한다."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.ingestion.fault_standards import build_standards
from app.ingestion.preprocess import preprocess_chunks


def main():
    parser = argparse.ArgumentParser(
        description="① 인정기준 도표·속성 후보·수정요소·청크 일괄 생성"
    )
    parser.add_argument("blocks", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    blocks = [
        json.loads(line)
        for line in args.blocks.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if any(b["document"]["corpus"] != "fault_standards" for b in blocks):
        parser.error("fault_standards 코퍼스만 처리할 수 있습니다.")
    identities = {(b["document"]["document_id"], b["document"]["content_hash"]) for b in blocks}
    if len(identities) != 1:
        parser.error("원문 하나의 블록만 입력하세요.")
    result = build_standards(blocks)
    result["chunks"] = preprocess_chunks(result["chunks"])
    result["summary"]["chunks"] = len(result["chunks"])
    args.output.mkdir(parents=True, exist_ok=True)
    for name, rows in result.items():
        if name == "summary":
            (args.output / "summary.json").write_text(
                json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        else:
            (args.output / f"{name}.jsonl").write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
            )
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
