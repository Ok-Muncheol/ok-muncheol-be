"""인정기준 도표 테이블을 구조화한다. 자동 추출값은 검수 전 계산에 사용하지 않는다."""

import hashlib
import json
import re
from collections import Counter

from app.schemas.retrieval import ParsedBlock

CHART = re.compile(r"차\d+-\d+")
HEADINGS = re.compile(
    r"(?m)^\s*(사고 상황|기본 과실비율 해설|수정요소[^\n]*해설|활용시 참고 사항|관련 법규|참고 판례)\s*$"  # noqa: E501 -- source text or prompt kept intact
)


def compact(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def render_chart_card(record, modifiers):
    """검수용 구조값을 읽을 수 있는 문장으로 표현한다. 새 조건/수치를 추론하지 않는다."""
    lines = [f"도표 {record['chart_id']}: {record['title']}"]
    lines += [
        f"당사자 {side}: {value}" for side, value in record["party_descriptions"].items() if value
    ]
    for ratio in record["base_variants"]:
        label = f"({ratio['variant']}) " if ratio["variant"] else ""
        lines.append(f"기본 과실비율 {label}A {ratio['base_A']} : B {ratio['base_B']}")
    lines.append("수정요소 (적용 조건은 원문 해설 확인):")
    for item in modifiers:
        value = item.get("resolved_value", item["value_raw"])
        lines.append(
            f"{item['party']} {item['name']}: {value if value is not None else '원문 셀 확인 필요'}"
        )
    return "\n".join(lines)


def build_standards(blocks: list[ParsedBlock]) -> dict[str, object]:
    """도표 ID가 첫 셀에 있는 표만 도표로 인정. 목차·판례의 ID 언급은 제외."""
    texts = {b["page"]: b["text"] for b in blocks if b["kind"] == "text"}
    tables = []
    for block in blocks:
        if block["kind"] != "table":
            continue
        table = json.loads(block["text"])
        if table and table[0] and CHART.fullmatch((table[0][0] or "").strip()):
            tables.append((block, table))
    if not tables:
        raise ValueError("차대차 도표 테이블을 찾지 못했습니다.")
    page_counts = Counter(b["page"] for b, _ in tables)
    ids = [t[0][0].strip() for _, t in tables]
    if len(set(ids)) != len(ids):
        raise ValueError("도표 ID가 중복됩니다. 다른 적용판은 별도 처리하세요.")
    last_page = max(texts)
    # 다음 장 표지 전까지만 차대차 설명으로 연결한다. 부록 혼입 방지.
    for page in sorted(texts):
        if page > tables[-1][0]["page"] and "과실비율 적용기준(사고유형별)" in texts[page]:
            last_page = page - 1
            break
    records, chunks, modifiers, review, attributes = [], [], [], [], []
    reference_ids = []
    # 본문의 수정요소 해설 참조를 페이지 근거로 연결한다.
    reference_start = next(
        (
            p
            for p in sorted(texts)
            if 100 < p < tables[0][0]["page"]
            and re.search(r"3\.\s*수정요소", texts[p])
            and "해설" in texts[p]
            and re.search(r"(?m)^3\.\s*수정요소", texts[p])
        ),
        None,
    )
    if reference_start is not None:
        for p in range(reference_start, tables[0][0]["page"]):
            parent = f"{tables[0][0]['document']['document_id']}:modifier-guide:{p}"
            reference_ids.append(parent)
            chunks.append(
                {
                    "chunk_id": hashlib.sha256(parent.encode()).hexdigest()[:24],
                    "parent_id": parent,
                    "document": tables[0][0]["document"],
                    "page_start": p,
                    "page_end": p,
                    "text": texts[p],
                    "metadata": {
                        "section": "common_modifier_guide",
                        "review_status": "unreviewed",
                        "embedding_ready": False,
                    },
                }
            )
    for block, table in tables:
        page = block["page"]
        following = [b["page"] for b, _ in tables if b["page"] > page]
        end = min(following) - 1 if following else last_page
        chart_id = table[0][0].strip()
        doc = block["document"]
        parent_id = f"{doc['document_id']}:{chart_id}"
        issues = (
            ["unreviewed_source", "effective_period_unconfirmed"]
            if not doc["effective_from"]
            else ["unreviewed_source"]
        )
        title = compact(next((c for c in table[0][1:] if c), ""))
        description = "\n".join(c for row in table[1:2] for c in row if c)
        party = {}
        for side in "AB":
            m = re.search(rf"\({side}\)\s*(.*?)(?=\([AB]\)|\Z)", description, re.S)
            if not m:
                m = re.search(
                    rf"자동차\s*{side}\s*[:：]\s*(.*?)(?=자동차\s*[AB]\s*[:：]|\Z)",
                    description,
                    re.S,
                )
            party[side] = compact(m.group(1)) if m else None
        if any(value is None for value in party.values()):
            issues.append("party_description_unresolved")
        ratio_rows = [row for row in table if any(c and "기본 과실비율" in c for c in row)]
        variants = []
        if len(ratio_rows) == 1:
            row = ratio_rows[0]
            a = [int(x) for c in row if c for x in re.findall(r"A\s*(\d{1,3})(?!\d)", c)]
            b = [int(x) for c in row if c for x in re.findall(r"B\s*(\d{1,3})(?!\d)", c)]
            # 그림 영역에도 (가)가 등장하므로 비율 바로 왼쪽의 조건 셀만 읽는다.
            a_columns = [i for i, c in enumerate(row) if c and re.search(r"A\s*\d", c)]
            label_cell = row[a_columns[0] - 1] if a_columns else None
            labels = re.findall(r"\(([가-힣])\)", label_cell or "")
            if len(a) == len(b) and a and all(x + y == 100 for x, y in zip(a, b, strict=False)):
                for i, (av, bv) in enumerate(zip(a, b, strict=False)):
                    label = labels[i] if len(labels) == len(a) else None
                    variants.append(
                        {
                            "variant": label,
                            "base_A": av,
                            "base_B": bv,
                            "condition_raw": description,
                            "review_status": "unreviewed",
                        }
                    )
                if len(a) > 1 and len(labels) != len(a):
                    issues.append("variant_label_alignment")
        if not variants:
            issues.append("base_ratio_unresolved")
        modifier_rows = []
        started = False
        for row in table:
            if any(c and "기본 과실비율" in c for c in row):
                started = True
                continue
            if not started:
                continue
            candidates = [
                (i, c)
                for i, c in enumerate(row)
                if c and re.match(r"^[AB](?=[가-힣\s(])", c.strip())
            ]
            if len(candidates) != 1:
                if any(c and re.search(r"[+-]\d|비적용", c) for c in row):
                    issues.append("modifier_row_unresolved")
                continue
            index, name = candidates[0]
            values = [c.strip() for c in row[index + 1 :] if c and c.strip()]
            value = values[0] if len(values) == 1 else None
            raw_value = value
            resolution = "direct_cell"
            # 이 원문의 두 도표는 B 좌회전 완료직후~소좌회전·대좌회전이
            # 하나의 '비적용' 셀을 공유한다. PDF 164/167 시각 대조 완료.
            if (
                doc["content_hash"]
                == "6502fca09a6f9cb26c6166f182429f9c947582dbdbbe1e844a3c7949fbfab06e"
                and chart_id in ("차2-1", "차2-2")
                and compact(name) in ("B 서행불이행", "B 급좌회전", "B 소좌회전·대좌회전")
                and value is None
            ):
                value = "비적용"
                resolution = "visually_verified_merged_cell"
            delta = int(value) if value and re.fullmatch(r"[+-]\d+", value) else None
            item = {
                "modifier_id": f"{parent_id}:modifier:{len(modifier_rows) + 1}",
                "parent_id": parent_id,
                "chart_id": chart_id,
                "party": name.strip()[0],
                "name": compact(name.strip()[1:]),
                "delta_percentage_points": delta,
                "applicability": "not_applicable"
                if value == "비적용"
                else "unresolved"
                if delta is None
                else "conditional",
                "value_raw": raw_value,
                "resolved_value": value,
                "row_raw": row,
                "source_pdf_page": page,
                "extraction_resolution": resolution,
                "review_status": "unreviewed",
            }
            if delta is None and value != "비적용":
                issues.append("modifier_value_or_merged_cell_unresolved")
            modifier_rows.append(item)
        modifiers.extend(modifier_rows)
        # 속성은 원문에 나타난 표현만 수집. 신호 진입/충돌 시점 및 부정은 추론하지 않는다.
        properties = {}
        for side, raw in party.items():
            # 참고용 낱말 후보. "후진입"(진입 순서)을 후진으로 잡지 않는다.
            properties[f"movement_{side}"] = [
                v
                for v in ("직진", "좌회전", "우회전", "유턴", "후진", "진로변경", "정차", "주차")
                if raw and re.search(rf"{v}(?!입)" if v == "후진" else re.escape(v), raw)
            ]
            properties[f"party_description_{side}"] = raw
        properties["title_terms"] = [
            v
            for v in (
                "교차로",
                "회전교차로",
                "고속도로",
                "주차장",
                "동일폭",
                "대소로",
                "신호",
                "추돌",
                "차로 변경",
            )
            if v in title
        ]
        # 사실 카드 칸 이름(signal_A/B, app/schemas/chart_scope.py)과 같게 둔다.
        # 원문에서 값을 뽑지 않아 null이다.
        properties["signal_A"] = None
        properties["signal_B"] = None
        for field, values in properties.items():
            if not isinstance(values, list):
                continue
            side = field[-1] if field.startswith("movement_") else None
            for value in values:
                attributes.append(
                    {
                        "parent_id": parent_id,
                        "chart_id": chart_id,
                        "field": field,
                        "value": value,
                        "evidence_text": party[side] if side else title,
                        "source_pdf_page": page,
                        "review_status": "unreviewed",
                        "hard_filter_ready": False,
                    }
                )
        record = {
            "parent_id": parent_id,
            "chart_id": chart_id,
            "document": doc,
            "title": title,
            "party_descriptions": party,
            "page_start": page,
            "page_end": end,
            "base_variants": variants,
            "modifier_ids": [m["modifier_id"] for m in modifier_rows],
            "filter_candidates": properties,
            "condition_json": None,
            "unknown_attribute_semantics": "빈 목록/null은 불명이며 조건 불일치를 뜻하지 않음",
            "modifier_guide_parent_ids": reference_ids,
            "filter_status": "unreviewed_candidates_only",
            "calculation_ready": False,
            "raw_chart_table": table,
            "review_status": "unreviewed",
            "source_relationship": "shared_explanations"
            if page_counts[page] > 1
            else "single_chart",
            "issues": sorted(set(issues)),
        }
        records.append(record)
        # 페이지 원문을 유지하고 설명 제목으로 나눈다. 표 JSON은 청크에 중복 삽입하지 않는다.
        for p in range(page, end + 1):
            text = texts[p]
            matches = list(HEADINGS.finditer(text))
            spans = [("page_context", 0, matches[0].start() if matches else len(text))]
            spans += [
                (m.group(1), m.end(), matches[i + 1].start() if i + 1 < len(matches) else len(text))
                for i, m in enumerate(matches)
            ]
            for section, start, stop in spans:
                body = text[start:stop].strip()
                display_text = None
                if p == page and section == "page_context":
                    # 표를 원문 그대로 직렬화해 인접 도표/장식문자 혼입을 차단한다.
                    display_text = render_chart_card(record, modifier_rows)
                    body = json.dumps(table, ensure_ascii=False)
                if not body:
                    continue
                # 같은 페이지의 여러 도표는 공유 원문임을 표시한다.
                chunk_id = hashlib.sha256(
                    f"{parent_id}:{p}:{section}:{start}".encode()
                ).hexdigest()[:24]
                chunks.append(
                    {
                        "chunk_id": chunk_id,
                        "parent_id": parent_id,
                        "document": doc,
                        "page_start": p,
                        "page_end": p,
                        "text": f"{chart_id} {title}\n{section}\n{body}",
                        "display_text": display_text,
                        "metadata": {
                            "chart_id": chart_id,
                            "section": section,
                            "filter_candidates": properties,
                            "review_status": "unreviewed",
                            "shared_source": page_counts[page] > 1,
                            "related_parent_ids": [
                                f"{doc['document_id']}:{t[0][0].strip()}"
                                for b, t in tables
                                if b["page"] == page
                            ],
                            "embedding_ready": False,
                        },
                    }
                )
        review.append(
            {"parent_id": parent_id, "chart_id": chart_id, "page": page, "issues": record["issues"]}
        )
    chart_pages = {b["page"] for b, _ in tables}
    missing = [
        p
        for p, text in texts.items()
        if tables[0][0]["page"] <= p <= last_page
        and "기본 과실비율" in text
        and re.search(r"(?m)^차\d+-\d+\s*$", text)
        and p not in chart_pages
    ]
    return {
        "charts": records,
        "modifiers": modifiers,
        "chunks": chunks,
        "attributes": attributes,
        "review_queue": review,
        "summary": {
            "charts": len(records),
            "modifiers": len(modifiers),
            "chunks": len(chunks),
            "page_start": tables[0][0]["page"],
            "page_end": last_page,
            "possible_missing_chart_pages": missing,
            "issue_counts": dict(Counter(i for r in records for i in r["issues"])),
            "status": "unreviewed",
            "hard_filter_ready": False,
        },
    }
