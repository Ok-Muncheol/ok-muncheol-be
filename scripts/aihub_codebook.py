"""AI Hub 교통사고 영상 데이터(597) 설명서 PDF의 사고 유형 코드표(0~433)를 JSON으로 뽑는다.

설명서는 data/private/aihub/ 에 두며 저장소에 커밋하지 않는다. 출력도 같은 폴더에 둔다.
    python scripts/aihub_codebook.py "data/private/aihub/<설명서>.pdf" \
        data/private/aihub/codebook.json
pdfplumber가 필요하다: pip install -e ".[vision-eval]"
"""

import json
import re
import sys
from pathlib import Path

OBJECT_LABELS = {"차대차", "차대보행자", "차대자전거", "차대 이륜차", "차대이륜차"}

# 페이지 경계에서 표 추출이 놓치는 행. 설명서 본문(텍스트)에서 그대로 옮겼다.
MANUAL_ROWS: dict[int, dict[str, object]] = {
    94: {"place": "사거리교차로(신호등 있음)", "feature": "유턴 구역",
         "a": "우회전(좌측도로)", "b": "신호에 따른 유턴", "rateA": 80, "rateB": 20},
    140: {"place": "횡단보도(신호등 있음)", "feature": "자동차 교차로 통과 후 충격",
          "a": "[적색신호] 횡단 시작, [적색신호]", "b": "[황색 우회전 신호] 우회전",
          "rateA": 50, "rateB": 50},
}


def extract_rows(pdf_path: Path) -> dict[int, dict[str, object]]:
    import pdfplumber

    table: dict[int, dict[str, object]] = {}
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for rows in page.extract_tables():
                for row in rows:
                    cells = [(c or "").replace("\n", " ").strip() for c in row]
                    cells = [c for c in cells if c]
                    if len(cells) < 7 or not re.fullmatch(r"\d{1,3}", cells[-1]):
                        continue
                    if cells[0] in OBJECT_LABELS:
                        cells = cells[1:]
                    place, feature, a, b, rate_a, rate_b, code = cells[-7:]
                    table[int(code)] = {
                        "place": place, "feature": feature, "a": a, "b": b,
                        "rateA": int(rate_a), "rateB": int(rate_b),
                    }
    for code, row in MANUAL_ROWS.items():
        table.setdefault(code, row)
    return table


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    pdf_path, out_path = Path(sys.argv[1]), Path(sys.argv[2])
    table = extract_rows(pdf_path)
    missing = sorted(set(range(434)) - set(table))
    payload = {
        "source": pdf_path.name,
        "accident_type": {str(k): table[k] for k in sorted(table)},
        "missing_codes": missing,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"{len(table)} codes -> {out_path}; missing {missing}")


if __name__ == "__main__":
    main()
