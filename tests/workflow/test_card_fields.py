"""B: 카드 v1 확정본(영어 12칸)·한국어 안·이전 형식(영어 값) → 도표 선택 칸·값 변환과 검색 문장."""

import json
from pathlib import Path

import pytest

from app.schemas.chart_scope import FIELD_OPTIONS
from app.schemas.facts import FACT_FIELD_OPTIONS
from app.workflow.candidates import build_search_query, confirmed_values
from app.workflow.card_fields import (
    card_field_key,
    card_format,
    chart_fields,
    chart_key,
    confirmed_unknown_fields,
    is_v1_card,
    question_options,
    to_card_terms,
)
from tests.workflow.synthetic import card, en_v1_card, field, v1_card

EXAMPLE = Path(__file__).resolve().parents[2] / "docs/contracts/fact-card-v1.example.json"


def test_v1_card_reads_as_the_same_chart_values_as_the_old_card():
    # 같은 사고(신호 교차로, A 녹색 직진 대 B 적색 좌회전, 주간)를 두 형식으로 쓰면 같은 값이다.
    # 한국어 안의 상대 위치(맞은편)는 도표 값으로 바꾸지 않아 `*`과만 맞는다(11/6 카드엔 없다)
    values = confirmed_values(v1_card())
    assert values.pop("relative_position") == "맞은편"
    assert values == confirmed_values(card()) == {
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


# ── 카드 v1 확정본(영어 12칸, #120) ─────────────────────────────────────


def test_card_formats():
    assert card_format(card()) == "legacy"
    assert card_format(v1_card()) == "v1_ko"
    assert card_format(en_v1_card()) == "v1"
    assert card_format(json.loads(EXAMPLE.read_text(encoding="utf-8"))) == "v1"
    # #119는 relative_position 칸이 있으면 한국어 안으로 봐서 확정본에 한국어로 물었다
    assert not is_v1_card(en_v1_card())


def test_en_v1_card_reads_as_the_same_chart_values_as_the_old_card():
    # 상대 위치는 v1에서 생긴 도표 선택 칸이다(11/6 카드에는 없다)
    values = confirmed_values(en_v1_card())
    assert values.pop("relative_position") == "opposite"
    assert values == confirmed_values(card())
    # 도표 A = 카드 B(촬영 차량)여도 같은 사고면 같은 도표 선택 값이다(v1 2절). 맞은편은 그대로다
    swapped = confirmed_values(en_v1_card(swapped=True))
    assert swapped.pop("relative_position") == "opposite"
    assert swapped == confirmed_values(card())


def test_en_v1_questions_use_chart_field_names_and_v1_values():
    v1 = en_v1_card()
    assert card_field_key("accident_type", v1) == "accident_type"
    assert card_field_key("night", v1) == "night"
    assert question_options("accident_type", v1) == ["car_to_car", "other"]
    assert question_options("road_priority", v1) == [
        "same_width", "major_A", "major_B", "stop_A", "stop_B", "other", "none"]
    assert question_options("entry_order", v1) == ["first", "second", "simultaneous", "none"]
    assert question_options("movement_B", v1)[-2:] == ["lane_change", "other"]


def test_en_v1_confirmed_null_is_unknown_and_unconfirmed_null_is_not():
    c = en_v1_card(movement_B=field(None), signal_B=field(None, confirmed=False))
    # 담당자가 확인한 null만 '모름'이다. 모델이 몰라 needs_review인 칸은 질문 대상이다
    assert confirmed_unknown_fields(c) == ["movement_B"]
    values = confirmed_values(c)
    assert "movement_B" not in values and "signal_B" not in values


@pytest.mark.parametrize(("value", "expected"), [
    ("straight_road", "other"), ("highway", "other"), ("other", "other"),
    ("roundabout", "roundabout"), ("unsignalized_intersection", "unsignalized_intersection"),
])
def test_en_v1_locations(value, expected):
    # 직선도로·고속도로는 v1에서 따로 내지만 MVP 범위 밖 보류는 그대로다(v1 8절)
    assert confirmed_values(en_v1_card(location_type=field(value)))["location_type"] == expected


@pytest.mark.parametrize(("key", "value"), [
    ("road_priority", "major_A"), ("road_priority", "stop_B"), ("road_priority", "none"),
    ("road_priority", "other"), ("entry_order", "none"), ("movement_A", "lane_change"),
    ("movement_A", "other"),
])
def test_en_v1_values_without_a_chart_value_stay_as_they_are(key, value):
    # 쪽이 붙은 도로 관계를 major_minor·stop_sign으로 바꾸면 A/B가 뒤집힌 도표를 고를 수 있다
    assert confirmed_values(en_v1_card(**{key: field(value)}))[key] == value


def test_swapped_en_v1_card_reads_chart_A_from_card_B():
    c = en_v1_card(swapped=True, road_priority=field("major_A"), entry_order=field("first"))
    values = confirmed_values(c)
    # 카드 A(상대 차량)가 도표 B다. 도로 관계·진입 순서의 쪽도 뒤집는다
    assert (values["road_priority"], values["entry_order"]) == ("major_B", "second")
    # 질문은 카드 쪽 칸 이름으로 낸다. 답이 그 칸 그대로 카드 수정으로 간다
    assert card_field_key("movement_A", c) == "movement_B"
    assert card_field_key("road_priority", c) == "road_priority"
    assert chart_key("movement_B", c) == "movement_A"
    # 가정 실행은 도표 선택 칸·값을 카드 쪽으로 되돌려 넣는다
    assert to_card_terms("movement_A", "straight", c) == ("movement_B", "straight")
    assert to_card_terms("entry_order", "first", c) == ("entry_order", "second")
    assert to_card_terms("movement_A", "straight", en_v1_card()) == ("movement_A", "straight")
    assert chart_key("movement_B", card()) == "movement_B"


def test_only_v1_cards_are_swapped():
    # 이전 형식·한국어 안은 카드 A/B를 도표 A/B로 본다. 촬영 차량 표시가 없는 v1도 그대로다
    old = card()
    old["party_mapping"] = {**old["party_mapping"], "video_ego_party_id": "p-1"}
    assert confirmed_values(old) == confirmed_values(card())
    manual = en_v1_card(swapped=True)
    manual["party_mapping"] = {**manual["party_mapping"], "video_ego_party_id": None}
    assert confirmed_values(manual)["movement_A"] == "left_turn"


def test_example_card_from_the_vision_pipeline():
    c = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    for f in c["fields"].values():  # 담당자가 모델 값을 그대로 확정했다고 본다
        f["verification_status"] = "human_confirmed"
    assert confirmed_values(c) == {
        "accident_type": "car_to_car", "location_type": "unsignalized_intersection",
        "road_priority": "same_width", "signal_A": "no_signal", "signal_B": "no_signal",
        "movement_A": "straight", "movement_B": "straight", "relative_position": "right",
        "night": "true",
    }
    # 진입 순서는 모델이 몰랐고(null) 담당자가 그대로 확정했다 → '모름'
    assert confirmed_unknown_fields(c) == ["entry_order"]
    assert "불명" not in build_search_query(c)["text"]


def test_chart_values_follow_the_v1_card():
    """도표 선택 값(C 레코드 값)은 카드 v1 확정본 값과 같다. 범위 판정 칸(사고 유형)만 좁다."""
    for key in ("road_priority", "signal_A", "signal_B", "movement_A", "movement_B",
                "relative_position", "entry_order"):
        assert FIELD_OPTIONS[key] == FACT_FIELD_OPTIONS[key], key
    assert set(FIELD_OPTIONS["accident_type"]) < set(FACT_FIELD_OPTIONS["accident_type"])


def test_swapped_v1_card_flips_position_and_priority_side():
    """도표 A가 촬영 차량이면 상대 위치 좌우·앞뒤와 우선 도로 쪽도 뒤집는다(맞은편은 그대로)."""
    c = en_v1_card(swapped=True, relative_position=field("left"), road_priority=field("stop_B"))
    values = confirmed_values(c)
    assert (values["relative_position"], values["road_priority"]) == ("right", "stop_A")
    assert to_card_terms("relative_position", "right", c) == ("relative_position", "left")
    front = en_v1_card(swapped=True, relative_position=field("same_direction_front"))
    assert confirmed_values(front)["relative_position"] == "same_direction_rear"


def test_v1_value_alone_still_marks_a_v1_card():
    """도표 선택 값이 v1 값으로 바뀌어도 11/6 값에 없던 값(major_A 등)이 있으면 확정본 카드다."""
    c = card()
    c["fields"]["road_priority"] = field("major_A")
    assert card_format(c) == "v1"
