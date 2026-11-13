"""B: 판단에 넘기는 카드의 사고 일자.

영상 카드는 사고 일자 없이 온다(분석 작업에 사건 일자를 넘기지 않는다). 판단은 사건의 사고 일시를
KST 날짜(YYYY-MM-DD)로 바꿔 채운다. 검색 도구와 적용판 검사가 그 형식만 받기 때문이다.
DB가 필요 없는 부분만 본다. 실제 Postgres 흐름은 test_judgment_service.py가 본다.
"""

import asyncio
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.api import judgments
from tests.workflow.synthetic import card


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-31", "2026-10-31"),
        ("2026-10-31T14:10:00+09:00", "2026-10-31"),
        ("2026-10-31T15:30:00Z", "2026-11-01"),  # UTC 15:30 = KST 다음 날 00:30
        (datetime(2026, 10, 31, 15, 30, tzinfo=UTC), "2026-11-01"),
        (datetime(2026, 10, 31, 23, 50), "2026-10-31"),  # 시간대 없는 값은 그 날짜 그대로
        (date(2026, 10, 31), "2026-10-31"),
        (None, None),
        ("", None),
        ("어제", None),
        (20261031, None),
    ],
)
def test_accident_day(value, expected):
    assert judgments.accident_day(value) == expected


def fill(c: dict, case_date: object) -> tuple[dict, AsyncMock]:
    lookup = AsyncMock(return_value=case_date)
    with patch.object(judgments.runs, "case_accident_date", lookup):
        out = asyncio.run(judgments._with_case_accident_date(object(), "case-1", c))  # type: ignore[arg-type]
    return out, lookup


def test_missing_card_date_comes_from_the_case_in_kst():
    c = {**card(), "accident_date": None}
    out, lookup = fill(c, datetime(2026, 10, 31, 15, 30, tzinfo=UTC))
    assert out["accident_date"] == "2026-11-01"
    assert c["accident_date"] is None  # 원본 카드는 바꾸지 않는다
    lookup.assert_awaited_once()


def test_card_date_wins_and_is_cut_to_the_day():
    c = {**card(), "accident_date": "2026-10-31T14:10:00+09:00"}
    out, lookup = fill(c, datetime(2026, 1, 1, tzinfo=UTC))
    assert out["accident_date"] == "2026-10-31"
    lookup.assert_not_awaited()


def test_no_date_anywhere_stays_empty():
    out, _ = fill({**card(), "accident_date": None}, None)
    assert out["accident_date"] is None
