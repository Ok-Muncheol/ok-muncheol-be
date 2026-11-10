"""약관 보험금 지급기준 별표 — 합성 좌표·문구만 쓴다. 실제 약관 원문은 넣지 않는다."""

import pytest

from app.ingestion import policy_appendix as pa

DOC = {"document_id": "synthetic-policy"}


def line(x0, x1, top, bottom=None):
    return {"x0": x0, "x1": x1, "top": top, "bottom": top if bottom is None else bottom}


def test_table_rows_use_item_body_separator_pairs():
    lines = [
        # 행 구분선: 항목 칸(25–70) + 지급 기준 칸(70–280)
        *(seg for y in (45, 57, 213, 443) for seg in (line(25, 70, y), line(70, 280, y))),
        # 왼쪽 세로선은 첫 표(45–213)와 둘째 행(213–443)에 있다
        line(25, 25, 45, 213),
        line(25, 25, 213, 443),
        # 본문 안 산식 상자: 짝이 없는 가로선이라 행이 아니다
        line(90, 270, 100),
        line(90, 270, 120),
    ]
    rows = pa.table_rows(lines)
    assert [(round(top), round(bottom)) for top, bottom, *_ in rows] == [
        (45, 57),
        (57, 213),
        (213, 443),
    ]
    assert rows[0][2:] == (25, 70, 280)


def test_gap_between_tables_is_not_a_row():
    lines = [
        *(seg for y in (29, 97, 148, 478) for seg in (line(34, 76, y), line(76, 290, y))),
        line(35, 35, 29, 97),
        line(35, 35, 148, 478),
    ]
    rows = pa.table_rows(lines)
    assert [(top, bottom) for top, bottom, *_ in rows] == [(29, 97), (148, 478)]


def free(page, *lines):
    return {"kind": "free", "page": page, "lines": list(lines)}


def row(page, item, *lines):
    return {"kind": "row", "page": page, "item": item, "lines": list(lines)}


def test_items_keep_date_split_rows_and_skip_header_row():
    items = pa.assemble_items(
        [
            free(3, "<별표 3> 합성 상계"),
            row(3, "항목", "지급 기준"),
            row(3, "1. 상계 (2022년 12월 31일 이전 사고에 적용)", "가. 이전 방법"),
            row(3, "1. 상계 (2023년 1월 1일 이후 사고에 적용)", "가. 이후 방법"),
            row(3, "2. 손익", "다른 이익은 상계함."),
        ]
    )
    assert [i["label"] for i in items] == [
        "1. 상계 (2022년 12월 31일 이전 사고에 적용)",
        "1. 상계 (2023년 1월 1일 이후 사고에 적용)",
        "2. 손익",
    ]
    records, chunks = pa.build_schedule(items, DOC)
    assert [r["parent_id"].split(":", 2)[2] for r in records] == [
        "별표3:1:until-2022-12-31",
        "별표3:1:from-2023-01-01",
        "별표3:2",
    ]
    assert records[0]["accident_date_to"] == "2022-12-31"
    assert records[1]["accident_date_from"] == "2023-01-01"
    assert chunks[1]["metadata"]["accident_date_from"] == "2023-01-01"
    assert chunks[2]["metadata"]["accident_date_status"] == "not_split_by_item"
    # 머리줄 뒤 빈 줄: 전처리가 머리줄과 본문을 합치지 않게 한다
    assert chunks[0]["text"].startswith(
        "<별표 3> 합성 상계 | 1. 상계 (2022년 12월 31일 이전 사고에 적용)\n\n"
    )


def test_row_continued_on_next_page_joins_same_item():
    items = pa.assemble_items(
        [
            free(1, "<별표 1> 합성 지급 기준", "가. 사 망", "합성 개요 문장"),
            row(1, "3. 수익", "가. 첫 쪽"),
            row(2, "", "이어지는 줄"),
            row(2, "3. 수익", "나. 둘째 쪽"),
            free(2, "나. 부상"),
            row(2, "1. 손해", "가. 부상 본문"),
            free(3, "<표> 합성 금액표", "1급 1만 원"),
        ]
    )
    labels = [
        (i["section"]["title"] if i["section"] else None, i["label"], i["pages"]) for i in items
    ]
    assert labels == [
        ("사망", "개요", [1]),
        ("사망", "3. 수익", [1, 2]),
        ("부상", "1. 손해", [2]),
        (None, "표1 합성 금액표", [3]),
    ]
    records, _ = pa.build_schedule(items, DOC)
    assert [r["parent_id"].split(":", 2)[2] for r in records] == [
        "별표1:가:개요",
        "별표1:가:3",
        "별표1:나:1",
        "별표1:표1",
    ]
    assert (records[1]["page_start"], records[1]["page_end"]) == (1, 2)


def test_long_item_splits_only_at_ordinal_heads():
    body = ["가. 첫 항목 " + "가" * 900, "액. 다만 줄바꿈 조각", "나. 둘째 항목 " + "나" * 900]
    items = pa.assemble_items([free(1, "<별표 2> 합성"), row(1, "1. 긴 항목", *body)])
    _, chunks = pa.build_schedule(items, DOC)
    assert len(chunks) == 2
    assert "액. 다만 줄바꿈 조각" in chunks[0]["text"]
    assert all(c["metadata"]["requires_parent_context"] for c in chunks)
    assert len({c["chunk_id"] for c in chunks}) == 2


PASSENGER_TEXT = "\n".join(
    [
        "동승자의 강요 및 무단 동승 100%",
        "음주운전, 무면허운전(*2), 마약·약물운전 또",
        "는 사고발생 시의 조치의무 위반 운전자의 40%",
        "차량 동승",
        "동승자의 요청 동승 30%",
        "상호 의논합의 동승 20%",
        "운전자의 권유 동승 10%",
        "운전자의 강요 동승 0%",
        "출·퇴근시간대 함께타기 미적용",
        "동승자의 동승과정에 과실이 있는 경우 ＋10 ~ 20%",
    ]
)


def test_passenger_reductions_match_rate_after_factor():
    records = pa.passenger_reduction_records(PASSENGER_TEXT, DOC, 4)
    assert [(r["percent_min"], r["percent_max"]) for r in records] == [
        (100, 100),
        (40, 40),
        (30, 30),
        (20, 20),
        (10, 10),
        (0, 0),
        (10, 20),
    ]
    assert records[-1]["sign"] == "+"
    assert all(
        r["calculation_ready"] is False and r["review_status"] == "unreviewed" for r in records
    )


def test_passenger_reduction_rejects_rate_not_next_to_factor():
    # 「0%」는 「100%」 안에도 있다. 요소 바로 뒤에 없으면 거절해야 한다.
    broken = PASSENGER_TEXT.replace("운전자의 강요 동승 0%", "운전자의 강요 동승")
    with pytest.raises(ValueError, match="운전자의 강요 동승"):
        pa.passenger_reduction_records(broken, DOC, 4)


def test_amount_table_needs_all_grades_and_columns():
    columns = pa.DISABILITY_COLUMNS
    lines = [f"{g}급 {g}만 원 {g * 2}만 원 {g * 3}만 원 1억 원" for g in range(1, 15)]
    records = pa.amount_table_records(lines, DOC, 5, "합성표", columns)
    assert len(records) == 56
    assert records[0]["amount_krw"] == 10_000 and records[3]["amount_krw"] == 100_000_000
    with pytest.raises(ValueError, match="1~14"):
        pa.amount_table_records(lines[:-1], DOC, 5, "합성표", columns)
    with pytest.raises(ValueError, match="열 4개"):
        pa.amount_table_records(["1급 1만 원 2만 원"], DOC, 5, "합성표", columns)
