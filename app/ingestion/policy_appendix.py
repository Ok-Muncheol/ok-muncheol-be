"""C: 삼성화재 2026.11.11 약관의 보험금 지급기준 별표(PDF p.91–105)와 인용 시행령 표(p.106–119).

별표1~3은 「항목 | 지급 기준」 두 칸 표다. 페이지 텍스트를 그대로 뽑으면
왼쪽 항목명이 오른쪽 본문 줄 사이에 끼므로(p.103 등), PDF의 칸 선 좌표로 행과 칸을 나눈다.
항목 단위로 부모 레코드·청크를 만들고, 계산에 쓰는 수치(별표4 동승자 감액,
별표5 보험가입금액)는 청크와 별도로 원문 대조 구조 레코드로 둔다.
구조 레코드는 기계 대조만 했다(review_status=unreviewed, calculation_ready=False).
"""

import re

LAYOUT_VERSION = "samsung-20261111-schedule-v1"
SCHEDULE_PAGES = range(91, 106)
CITED_SCHEDULES = [
    (
        range(106, 114),
        "자배법시행령별표1",
        "자동차손해배상 보장법 시행령 [별표1] 상해의 구분과 책임보험금의 한도금액",
    ),
    (
        range(114, 120),
        "자배법시행령별표2",
        "자동차손해배상 보장법 시행령 [별표2] 후유장애의 구분과 책임보험금의 한도금액",
    ),
]
# 왼쪽 옆 탭(짝수쪽 x<20)·오른쪽 옆 탭(홀수쪽 x>=292)을 뺀 본문 영역
CONTENT_X = (20, 292)
SPLIT_CHARS = 1500
MIN_PART_CHARS = 200
# 항·호 머리 글자. 정규식 [가-하]는 「가」~「하」 사이 모든 음절(예: 「액」)이라 쓰지 않는다.
ORDINALS = "가나다라마바사아자차카타파하"
PAGE_DECORATION = re.compile(r"^(?:개인용\s*애니카\s*자동차보험\s*\d+|\d{1,3})$")
DATE = re.compile(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일\s*(이전|이후)")


def table_rows(lines: list[dict]) -> list[tuple[float, float, float, float, float]]:
    """칸 선으로 표 행을 찾는다. (top, bottom, left, boundary, right) 목록.

    행 구분선은 같은 높이에서 「항목」 칸 선분과 「지급 기준」 칸 선분이 이어진 쌍이다.
    두 구분선 사이에 왼쪽 세로선이 있어야 행이다(표 사이 여백·본문 안 산식 상자는 행이 아니다).
    """
    flat = [ln for ln in lines if abs(ln["top"] - ln["bottom"]) < 1 and ln["x1"] - ln["x0"] > 20]
    vertical = [ln for ln in lines if abs(ln["x0"] - ln["x1"]) < 1 and ln["bottom"] - ln["top"] > 3]
    separators = {}
    for a in flat:
        if not 30 <= a["x1"] - a["x0"] < 80:
            continue
        for b in flat:
            if (
                abs(a["top"] - b["top"]) < 1.5
                and abs(a["x1"] - b["x0"]) < 2
                and b["x1"] - b["x0"] > 150
            ):
                separators.setdefault(round(a["top"]), (a["top"], a["x0"], a["x1"], b["x1"]))
    ordered = [separators[k] for k in sorted(separators)]
    rows = []
    for (top, left, boundary, right), (bottom, *_rest) in zip(ordered, ordered[1:], strict=False):
        middle = (top + bottom) / 2
        if any(
            abs(v["x0"] - left) < 2.5 and v["top"] - 1 <= middle <= v["bottom"] + 1
            for v in vertical
        ):
            rows.append((top, bottom, left, boundary, right))
    return rows


def _clean(text: str) -> list[str]:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in (text or "").splitlines()]
    return [line for line in lines if line and not PAGE_DECORATION.match(line)]


def page_segments(page, page_number: int) -> list[dict]:
    """한 쪽을 읽는 순서대로 「표 밖 글」(free)과 「표 행」(row)으로 나눈다.

    좌표는 pdfplumber 단위다.
    """
    visible = page.within_bbox(page.bbox).dedupe_chars(tolerance=1)

    def text(x0, x1, y0, y1):
        def inside(obj):
            if obj.get("object_type") != "char":
                return False
            x = (obj["x0"] + obj["x1"]) / 2
            y = (obj["top"] + obj["bottom"]) / 2
            return x0 <= x < x1 and y0 <= y < y1

        return visible.filter(inside).extract_text() or ""

    rows = table_rows(page.lines)
    segments, cursor = [], 0.0
    for top, bottom, left, boundary, right in rows:
        if top > cursor:
            free = _clean(text(*CONTENT_X, cursor, top))
            if free:
                segments.append({"kind": "free", "page": page_number, "lines": free})
        item = " ".join(_clean(text(left - 2, boundary, top, bottom)))
        body = _clean(text(boundary, right + 2, top, bottom))
        segments.append({"kind": "row", "page": page_number, "item": item, "lines": body})
        cursor = bottom
    free = _clean(text(*CONTENT_X, cursor, page.height))
    if free:
        segments.append({"kind": "free", "page": page_number, "lines": free})
    return segments


def accident_date_range(label: str) -> dict:
    """항목명의 「YYYY년 M월 D일 이전/이후 사고에 적용」 → 적용 사고일 범위. 없으면 빈 dict."""
    match = DATE.search(label)
    if not match:
        return {}
    day = f"{int(match[1]):04d}-{int(match[2]):02d}-{int(match[3]):02d}"
    return {"accident_date_to": day} if match[4] == "이전" else {"accident_date_from": day}


def _item_key(label: str) -> str:
    """항목 번호(1.), 절 글자(가.), 부속표 번호(표1), 그 밖에는 「개요」."""
    number = re.match(rf"^(\d+|[{ORDINALS}])\.", label) or re.match(r"^(표\d+)", label)
    base = number[1] if number else re.sub(r"\s+", "", label)[:20]
    window = accident_date_range(label)
    if "accident_date_to" in window:
        base += ":until-" + window["accident_date_to"]
    if "accident_date_from" in window:
        base += ":from-" + window["accident_date_from"]
    return base


def assemble_items(segments: list[dict]) -> list[dict]:
    """쪽별 조각을 별표 → 절 → 항목으로 묶는다. 쪽을 넘는 행은 항목명이 같거나 비면 잇는다."""
    items: list[dict] = []
    schedule = section = None
    current = None
    tables = 0

    def start(label, kind, page):
        nonlocal current
        current = {
            "schedule": schedule,
            "section": section,
            "label": label,
            "kind": kind,
            "lines": [],
            "pages": [page],
        }
        items.append(current)

    def add(line, page):
        if current is None:
            start("개요", "free", page)
        current["lines"].append((page, line))
        if page not in current["pages"]:
            current["pages"].append(page)

    for seg in segments:
        page = seg["page"]
        if seg["kind"] == "row":
            label, lines = seg["item"], seg["lines"]
            if label == "항목" or (lines and lines[0].startswith("지급 기준") and not label):
                continue
            same = (
                current is not None
                and current["kind"] == "row"
                and (not label or label == current["label"])
            )
            if not same:
                start(label, "row", page)
            for line in lines:
                add(line, page)
            continue
        for line in seg["lines"]:
            heading = re.match(r"^<별표\s*(\d+)>\s*(.+)$", line)
            if heading:
                schedule = {"number": int(heading[1]), "title": heading[2].strip()}
                section = current = None
                tables = 0
                continue
            if schedule is None:
                continue
            table = re.match(r"^<표>\s*(.+)$", line)
            if table:
                # 별표1 끝의 <표>는 절(사망·부상·후유장애) 밖 별표 전체 부속표다.
                tables += 1
                section = None
                start(f"표{tables} {table[1].strip()}", "free", page)
                continue
            sub = re.match(rf"^([{ORDINALS}])\.\s*(.+)$", line)
            if sub and schedule["number"] in (4, 5):
                # 별표4·5는 표 밖 「가./나.」가 곧 항목이다.
                section = {"letter": sub[1], "title": sub[2].strip()}
                start(f"{sub[1]}. {sub[2].strip()}", "free", page)
                continue
            if sub and schedule["number"] == 1 and _compact(sub[2]) in {"사망", "부상", "후유장애"}:
                section = {"letter": sub[1], "title": _compact(sub[2])}
                current = None
                tables = 0
                continue
            add(line, page)
    return [item for item in items if item["lines"]]


def _parts(item: dict) -> list[list[tuple[int, str]]]:
    """긴 항목은 본문의 「가./나./…」 머리에서 나눈다.

    짧으면 한 조각이고, 너무 작은 조각은 앞 조각에 붙인다.
    """
    lines = item["lines"]
    if sum(len(t) for _, t in lines) <= SPLIT_CHARS:
        return [lines]
    parts, buffer = [], []
    for page, line in lines:
        if re.match(rf"^[{ORDINALS}]\.\s", line) and buffer:
            parts.append(buffer)
            buffer = []
        buffer.append((page, line))
    if buffer:
        parts.append(buffer)
    merged: list[list[tuple[int, str]]] = []
    for part in parts:
        if merged and sum(len(t) for _, t in part) < MIN_PART_CHARS:
            merged[-1].extend(part)
        else:
            merged.append(part)
    return merged


def build_schedule(items: list[dict], doc: dict) -> tuple[list[dict], list[dict]]:
    """항목 → (부모 레코드, 청크). 부모 ID: <document_id>:payment_standard:별표N:<절>:<항목>."""
    import hashlib

    records, chunks = [], []
    for item in items:
        number = item["schedule"]["number"]
        section = item["section"]
        label = item["label"]
        key = ":".join(
            part
            for part in (
                f"별표{number}",
                section["letter"] if section and number == 1 else None,
                _item_key(label),
            )
            if part
        )
        parent = f"{doc['document_id']}:payment_standard:{key}"
        heading = " | ".join(
            part
            for part in (
                f"<별표 {number}> {item['schedule']['title']}",
                section["title"] if section and number == 1 else None,
                label,
            )
            if part
        )
        window = accident_date_range(label)
        metadata = {
            "clause_type": "payment_standard",
            "schedule": f"별표{number}",
            "schedule_title": item["schedule"]["title"],
            "section": section["title"] if section else None,
            "item": label,
            **window,
            "accident_date_status": "from_item_label" if window else "not_split_by_item",
            "layout_version": LAYOUT_VERSION,
            "retrieval_role": "primary_payment_standard",
            "review_status": "unreviewed",
            "embedding_ready": False,
        }
        body = "\n".join(line for _, line in item["lines"])
        records.append(
            {
                "parent_id": parent,
                "document": doc,
                "page_start": min(item["pages"]),
                "page_end": max(item["pages"]),
                "record_type": "payment_standard_item",
                "title": heading,
                "text": body,
                **{
                    k: v
                    for k, v in metadata.items()
                    if k not in ("retrieval_role", "embedding_ready")
                },
                "contract_applicability": "unconfirmed",
            }
        )
        parts = _parts(item)
        for j, part in enumerate(parts):
            pages = [page for page, _ in part]
            chunks.append(
                {
                    "chunk_id": hashlib.sha256(f"{parent}:{j}".encode()).hexdigest()[:24],
                    "parent_id": parent,
                    "document": doc,
                    "page_start": min(pages),
                    "page_end": max(pages),
                    # 빈 줄: preprocess가 긴 머리줄과 본문 첫 줄을 합쳐
                    # 「머리줄만 남은 청크」로 버리지 않게 한다.
                    "text": heading + "\n\n" + "\n".join(line for _, line in part),
                    "metadata": {**metadata, "requires_parent_context": len(parts) > 1},
                }
            )
    return records, chunks


def build_cited(pages: list[dict], doc: dict) -> tuple[list[dict], list[dict]]:
    """인용 시행령 표: 표마다 부모 하나, 쪽마다 청크 하나. pages는 {"page", "text"} 목록."""
    import hashlib

    records, chunks = [], []
    for span, key, title in CITED_SCHEDULES:
        rows = [p for p in pages if p["page"] in span and p["text"].strip()]
        if not rows:
            continue
        parent = f"{doc['document_id']}:cited_schedule:{key}"
        records.append(
            {
                "parent_id": parent,
                "document": doc,
                "page_start": rows[0]["page"],
                "page_end": rows[-1]["page"],
                "record_type": "cited_law_schedule",
                "title": title,
                "text": "\n".join(p["text"] for p in rows),
                "review_status": "unreviewed",
            }
        )
        for p in rows:
            chunks.append(
                {
                    "chunk_id": hashlib.sha256(f"{parent}:p{p['page']}".encode()).hexdigest()[:24],
                    "parent_id": parent,
                    "document": doc,
                    "page_start": p["page"],
                    "page_end": p["page"],
                    "text": title + "\n\n" + p["text"],
                    "metadata": {
                        "clause_type": "cited_law_schedule",
                        "schedule": key,
                        "retrieval_role": "auxiliary_reference",
                        "requires_parent_context": len(rows) > 1,
                        "review_status": "unreviewed",
                        "embedding_ready": False,
                    },
                }
            )
    return records, chunks


# 별표4 기준요소·수정요소. 값은 원문 표에서 옮겨 적고 build 시 원문 문자열과 다시 대조한다.
PASSENGER_REDUCTIONS = [
    ("base", "동승자의 강요 및 무단 동승", ["동승자의 강요 및 무단 동승"], 100, 100),
    (
        "base",
        "음주운전, 무면허운전, 마약·약물운전 또는 사고발생 시의 조치의무 위반 운전자의 차량 동승",
        ["음주운전", "무면허운전", "마약·약물운전", "조치의무 위반 운전자의"],
        40,
        40,
    ),
    ("base", "동승자의 요청 동승", ["동승자의 요청 동승"], 30, 30),
    ("base", "상호 의논합의 동승", ["상호 의논합의 동승"], 20, 20),
    ("base", "운전자의 권유 동승", ["운전자의 권유 동승"], 10, 10),
    ("base", "운전자의 강요 동승", ["운전자의 강요 동승"], 0, 0),
    (
        "modifier",
        "동승자의 동승과정에 과실이 있는 경우",
        ["동승자의 동승과정에 과실이 있는 경우"],
        10,
        20,
    ),
]
PASSENGER_NOTES = [
    (
        "출·퇴근시간대",
        "평일 출·퇴근시간대 실제 출·퇴근용 승용차 함께타기는 동승자 감액 비율을 적용하지 않음",
    ),
    (
        "무면허 사실을 알 수 있었던 사정이 명확한 경우",
        "무면허운전 감액은 동승자가 무면허 사실을 알 수 있었던 사정이 명확한 경우에만 적용",
    ),
]


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def passenger_reduction_records(page_text: str, doc: dict, page: int) -> list[dict]:
    """별표4 감액 비율 → 구조 레코드. 원문에 문구·비율이 없으면 ValueError."""
    compact = _compact(page_text)
    records = []
    for i, (kind, factor, phrases, low, high) in enumerate(PASSENGER_REDUCTIONS):
        if not all(_compact(p) in compact for p in phrases):
            raise ValueError(f"별표4 원문에 없는 감액 요소: {factor}")
        # 비율은 그 요소 문구 바로 뒤에 있어야 한다(「0%」가 「100%」 안에서 맞는 것을 막는다).
        rates = [f"＋{low}~{high}%", f"+{low}~{high}%"] if kind == "modifier" else [f"{low}%"]
        tail = _compact(phrases[-1])
        if not any(tail + _compact(rate) in compact for rate in rates):
            raise ValueError(f"별표4 원문에서 요소 뒤에 비율이 없다: {factor} {rates[0]}")
        records.append(
            {
                "record_id": f"{doc['document_id']}:별표4:{kind}:{i + 1}",
                "parent_id": None,
                "document": doc,
                "schedule": "별표4",
                "table": "동승자 유형별 감액 비율표",
                "value_type": "passenger_reduction_rate"
                if kind == "base"
                else "passenger_reduction_adjustment",
                "factor": factor,
                "percent_min": low,
                "percent_max": high,
                "sign": "+" if kind == "modifier" else None,
                "conditions": [note for key, note in PASSENGER_NOTES if _compact(key) in compact],
                "source_pdf_page": page,
                "check": "machine_matched_source_text",
                "review_status": "unreviewed",
                "calculation_ready": False,
            }
        )
    return records


AMOUNT = r"(\d[\d,]*만\s*원|\d+억\s*원)"


def _won(text: str) -> int:
    text = _compact(text)
    if text.endswith("억원"):
        return int(text[:-2]) * 100_000_000
    return int(text[:-2].replace(",", "")) * 10_000


def amount_table_records(
    lines: list[str], doc: dict, page: int, table: str, columns: list[dict]
) -> list[dict]:
    """「N급 금액…」 줄 → 급·열별 금액 레코드. 열 개수와 급 1~14가 맞지 않으면 ValueError."""
    records = []
    for line in lines:
        match = re.match(rf"^(\d+)급\s+((?:{AMOUNT}\s*)+)$", line)
        if not match:
            continue
        amounts = re.findall(AMOUNT, match[2])
        if len(amounts) != len(columns):
            raise ValueError(f"{table} {match[1]}급: 열 {len(columns)}개에 금액 {len(amounts)}개")
        for column, amount in zip(columns, amounts, strict=True):
            records.append(
                {
                    "record_id": f"{doc['document_id']}:{table}:{match[1]}급:{column['key']}",
                    "parent_id": None,
                    "document": doc,
                    "schedule": "별표5",
                    "table": table,
                    "value_type": "insured_amount_by_grade",
                    "grade": int(match[1]),
                    **{k: v for k, v in column.items() if k != "key"},
                    "amount_krw": _won(amount),
                    "amount_raw": re.sub(r"\s+", " ", amount),
                    "source_pdf_page": page,
                    "check": "machine_parsed_grade_rows",
                    "review_status": "unreviewed",
                    "calculation_ready": False,
                }
            )
    grades = sorted({r["grade"] for r in records})
    if grades != list(range(1, 15)):
        raise ValueError(f"{table}: 급 1~14가 모두 있어야 한다 {grades}")
    return records


INJURY_COLUMNS = [
    {
        "key": "1500만-until-2022-12-31",
        "insured_amount_option": "1,500만 원",
        "accident_date_to": "2022-12-31",
    },
    {
        "key": "1500만-from-2023-01-01",
        "insured_amount_option": "1,500만 원",
        "accident_date_from": "2023-01-01",
    },
    {
        "key": "3000만-until-2022-12-31",
        "insured_amount_option": "3,000만 원",
        "accident_date_to": "2022-12-31",
    },
    {
        "key": "3000만-from-2023-01-01",
        "insured_amount_option": "3,000만 원",
        "accident_date_from": "2023-01-01",
    },
]
DISABILITY_COLUMNS = [
    {"key": key, "insured_amount_option": label}
    for key, label in [
        ("1500만", "1,500만 원"),
        ("3000만", "3,000만 원"),
        ("5000만", "5,000만 원"),
        ("1억", "1억 원"),
    ]
]
