"""현재 chunks.jsonl만 갱신. 원문은 raw_text와 파싱 파일에 보존한다."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from embed_corpora import LOCATIONS  # noqa: E402 -- standalone CLI adds repository root before app imports

from app.ingestion.preprocess import preprocess_chunks  # noqa: E402 -- standalone CLI adds repository root before app imports


def main():
    for folder, version in LOCATIONS:
        path = ROOT / "data/private" / folder / version
        file = path / "chunks.jsonl"
        old = [json.loads(s) for s in file.read_text(encoding="utf-8").splitlines() if s.strip()]
        new = preprocess_chunks(old)
        temp = file.with_suffix(".jsonl.tmp")
        temp.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in new), encoding="utf-8"
        )
        temp.replace(file)
        summary_path = path / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["chunks"] = len(new)
        summary["preprocessing_version"] = "conservative-layout-v1"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            folder,
            "before",
            len(old),
            "after",
            len(new),
            "changed",
            sum(c["text"] != c["raw_text"] for c in new),
        )


if __name__ == "__main__":
    main()
