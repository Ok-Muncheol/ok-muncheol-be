"""B: 카드 v1(한국어 값)·이전 형식(영어 값) → 도표 선택 칸·값 변환과 검색 문장."""

import pytest

from app.schemas.chart_scope import FIELD_OPTIONS
from app.workflow.candidates import build_search_query, confirmed_values
from app.workflow.card_fields import (
    card_field_key,
    chart_fields,
    chart_key,
    confirmed_unknown_fields,
    is_v1_card,
    question_options,
)
from tests.workflow.synthetic import card, field, v1_card


def test_v1_card_reads_as_the_same_chart_values_as_the_old_card():
    # 같은 사고(신호 교차로, A 녹색 직진 대 B 적색 좌회전, 주간)를 두 형식으로 쓰면 같은 값이다
    assert confirmed_values(v1_card()) == confirmed_values(card()) == {
        "accident_type": "car_to_car", "location_type": "signalized_intersection",
        "movement_A": "straight", "movement_B": "left_turn",
        "signal_A": "green", "signal_B": "red", "night": "false",
    }


def test_old_card_passes_through_unchanged():
    old = card()
    assert chart_fields(old["fields"]) == {k: v for k, v in old["fields"].items()
                                           if k in FIELD_OPTIONS}


def test_old_field_name_wins_over_v1_alias():
    # 질의응답 가정 실행은 도표 선택 칸 이름(이전 이름)으로 값을 덮어쓴다
    c = v1_card(accident_type=field("car_to_car"), accident_object=field("차대이륜차"),
                night=field(True))
    values = confirmed_values(c)
    assert (values["accident_type"], values["night"]) == ("car_to_car", "true")


def test_confirmed_unknown_is_not_a_value():
    c = v1_card(movement_B=field("불명"), signal_B=field("불명", confirmed=False))
    assert "movement_B" not in confirmed_values(c)
    # 담당자가 확인한 불명만 '모름'이다. 모델 값 불명은 미확인 칸이라 질문 대상이다
    assert confirmed_unknown_fields(c) == ["movement_B"]
    assert confirmed_unknown_fields(card()) == []


@pytest.mark.parametrize(("key", "value"), [
    ("entry_order", "해당없음"), ("road_priority", "A대로"), ("road_priority", "B쪽_일시정지"),
    ("road_priority", "A쪽만_신호등"), ("movement_A", "진로변경"), ("signal_A", "녹색직진좌회전"),
])
def test_values_without_a_chart_value_stay_as_they_are(key, value):
    # 도표 값과 맞지 않아 그 칸으로 갈리는 도표는 빠진다. 쪽이 붙은 도로 관계를 major_minor 등으로
    # 바꾸면 A/B가 뒤집힌 도표를 고를 수 있다
    c = v1_card(**{key: field(value)})
    if key == "signal_A":
        c["fields"]["movement_A"] = field("진로변경")
    assert confirmed_values(c)[key] == value


@pytest.mark.parametrize(("location", "expected"), [
    ("사거리_신호없음", "unsignalized_intersection"), ("회전교차로", "roundabout"),
    ("직선도로", "other"), ("고속도로", "other"), ("주차장", "other"), ("차도와비차도", "other"),
])
def test_location_values(location, expected):
    assert confirmed_values(v1_card(location_type=field(location)))["location_type"] == expected


def test_t_intersection_is_signalized_only_with_confirmed_lights():
    def location(**signals):
        return confirmed_values(v1_card(location_type=field("T자형교차로"), **signals))[
            "location_type"]

    assert location(signal_A=field("신호없음"), signal_B=field("신호없음")) \
        == "unsignalized_intersection"
    assert location() == "signalized_intersection"  # 녹색·적색 확인
    assert location(signal_A=field("녹색", confirmed=False),
                    signal_B=field("적색", confirmed=False)) == "unsignalized_intersection"


@pytest.mark.parametrize(("movement", "expected"), [
    (field("좌회전"), "green_left_arrow"), (field("직진"), "green"),
    (field("좌회전", confirmed=False), "녹색직진좌회전"),
])
def test_green_straight_and_left_signal_follows_the_confirmed_movement(movement, expected):
    c = v1_card(signal_A=field("녹색직진좌회전"), movement_A=movement)
    assert confirmed_values(c).get("signal_A") == expected


def test_v1_card_detection():
    assert is_v1_card(v1_card())
    assert not is_v1_card(card())
    # 이전 형식 카드라도 v1 값으로 답한 칸이 있으면 v1로 묻는다
    mixed = card()
    mixed["fields"]["movement_B"] = field("좌회전")
    assert is_v1_card(mixed)


def test_questions_follow_the_card_vocabulary():
    v1, old = v1_card(), card()
    assert card_field_key("accident_type", v1) == "accident_object"
    assert card_field_key("night", v1) == "time_condition"
    assert card_field_key("movement_B", v1) == "movement_B"
    assert card_field_key("accident_type", old) == "accident_type"
    assert question_options("accident_type", v1) == ["차대차", "차대이륜차", "기타"]
    assert "불명" not in question_options("movement_B", v1)  # '모름' 응답이 대신한다
    assert question_options("entry_order", v1) == ["A먼저", "B먼저", "동시", "해당없음"]
    assert question_options("movement_B", old) == list(FIELD_OPTIONS["movement_B"])
    assert chart_key("accident_object") == "accident_type"
    assert chart_key("movement_B") == "movement_B"


def test_search_query_is_the_same_for_both_card_shapes():
    assert build_search_query(v1_card())["text"] == build_search_query(card())["text"] \
        == "신호 교차로 A 녹색 직진 B 적색 좌회전"


@pytest.mark.parametrize(("order", "word"), [
    ("A먼저", "선진입"), ("B먼저", "후진입"), ("동시", "동시 진입"), ("해당없음", None),
    ("불명", None),
])
def test_search_query_handles_every_v1_entry_order(order, word):
    # 11/11 이전에는 entry_order가 영어 값이 아니면 KeyError로 판단이 멈췄다
    c = v1_card(location_type=field("사거리_신호없음"), road_priority=field("동일폭"),
                signal_A=field("신호없음"), signal_B=field("신호없음"),
                movement_B=field("직진"), entry_order=field(order))
    text = build_search_query(c)["text"]
    assert text.startswith("신호 없는 교차로 동일폭 A 신호 없음 직진 B 신호 없음 직진")
    assert text.endswith(word) if word else not text.endswith("진입")
    assert "불명" not in text and "해당없음" not in text


def test_search_query_skips_values_without_search_words():
    c = v1_card(location_type=field("사거리_신호없음"), road_priority=field("A쪽_일시정지"),
                signal_A=field("신호없음"), signal_B=field("불명"), movement_A=field("진로변경"))
    text = build_search_query(c)["text"]
    assert text == "신호 없는 교차로 A 신호 없음 B 좌회전"
    assert "_" not in text
