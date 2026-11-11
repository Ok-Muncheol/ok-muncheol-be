"""B: 사실 카드 칸·값 → 도표 선택 칸·값(`chart_scope`). 판단 그래프는 카드를 이것으로 읽는다.

카드 형식은 셋이다(`card_format`).
- `v1`: 정지원 카드 v1 확정본(`docs/contracts/fact-card-v1.md`, 11/11 #120, 영어 값 12칸).
  칸 이름은 도표 선택 칸과 같다. 새 값(`major_A`·`straight_road`·`lane_change`·`none` 등)과
  새 칸(`relative_position`·`collision_time_ms`·`narrative`)이 있다.
  모름은 `value: null` + `human_confirmed`다. `night`는 11/6 카드처럼 JSON 참거짓이다.
- `v1_ko`: 11/11 오전 한국어 안(#119가 읽던 형식). 칸 이름 둘이 다르다
  (`accident_object`·`time_condition`). 확정본이 대체했지만 이미 만든 카드를 읽을 수 있게 둔다.
- `legacy`: 11/6 영어 값 카드. 그대로 통과한다.
`chart_scope`(도표 선택 값, C 도표 레코드 값)는 11/11부터 v1 확정본 값이다. 그래서 v1 카드는 값을
그대로 쓰고, 한국어 안 카드만 값을 바꿔 읽는다.

- 담당자가 확인한 모름(v1 `null`, 한국어 안 `불명`)은 '모른다' 응답과 같다. 다시 묻지 않고,
  그 칸이 `*`인 도표만 남는다. 안에서는 `UNKNOWN`(`불명`) 표시 하나로 다룬다.
- v1 값(`major_A`·`stop_B`·`relative_position`·`none`·`lane_change` 등)은 도표 레코드가 같은 값이나
  `*`를 적었을 때만 맞는다. 도로 관계는 우선 도로가 어느 쪽인지까지 맞아야 한다.
- 한국어 안의 `해당없음`과 대응하는 도표 값이 없는 한국어 값은 그대로 둬서 `*`과만 맞는다
  (fail-closed). 11/6 카드의 `major_minor`·`stop_sign`은 쪽이 없어 어떤 도표 값과도 맞지 않는다.
- v1 2절: 카드의 B는 촬영 차량이다. 도표 A가 카드 B(`chart_party_A_id` = `video_ego_party_id`)면
  A/B를 뒤집어 읽는다. `_A`·`_B` 칸을 맞바꾸고 `major_A`↔`major_B`, `stop_A`↔`stop_B`,
  `first`↔`second`, `left`↔`right`, `same_direction_front`↔`same_direction_rear`를 바꾼다
  (`opposite`는 그대로). 질문의 칸 이름도 카드 쪽 이름으로 되돌린다(`card_field_key`).
"""

from collections.abc import Mapping
from typing import Literal

from app.schemas.chart_scope import FIELD_OPTIONS
from app.schemas.facts import FACT_FIELD_OPTIONS, FactCard, FactField

CardFormat = Literal["v1", "v1_ko", "legacy"]

UNKNOWN = "불명"
NOT_APPLICABLE = "해당없음"

# 한국어 안 칸 이름 → 도표 선택 칸 이름. 나머지 도표 선택 칸은 이름이 같다
V1_FIELD_KEYS: dict[str, str] = {"accident_object": "accident_type", "time_condition": "night"}
_CARD_KEYS: dict[str, str] = {chart: card for card, chart in V1_FIELD_KEYS.items()}

# 한국어 안 값 목록(A `workers/vision/fields.py`와 같은 순서). 한국어 안 카드의 질문 선택지로 쓴다
_SIGNALS = ("녹색", "황색", "적색", "녹색좌회전", "녹색직진좌회전", "신호없음", UNKNOWN)
_MOVEMENTS = (
    "직진", "좌회전", "우회전", "유턴", "진로변경", "합류", "추월",
    "회전교차로진입", "회전교차로회전", "정차후출발", "주정차", "후진", "출차", UNKNOWN,
)
V1_OPTIONS: dict[str, tuple[str, ...]] = {
    "accident_object": ("차대차", "차대이륜차", "기타", UNKNOWN),
    "location_type": (
        "직선도로", "사거리_신호있음", "사거리_신호없음", "T자형교차로", "회전교차로", "고속도로",
        "주차장", "차도와비차도", UNKNOWN,
    ),
    "road_priority": (
        "동일폭", "A대로", "B대로", "A쪽_일시정지", "B쪽_일시정지", "A쪽만_신호등", "B쪽만_신호등",
        NOT_APPLICABLE, UNKNOWN,
    ),
    "signal_A": _SIGNALS,
    "signal_B": _SIGNALS,
    "movement_A": _MOVEMENTS,
    "movement_B": _MOVEMENTS,
    "entry_order": ("A먼저", "B먼저", "동시", NOT_APPLICABLE, UNKNOWN),
    "time_condition": ("주간", "야간", "새벽황혼", UNKNOWN),
}
_V1_VALUE_SET = frozenset(v for values in V1_OPTIONS.values() for v in values)

# v1 확정본 값(3절, A `app/schemas/facts.py` `FACT_FIELD_OPTIONS`). v1 카드의 질문 선택지로 쓴다.
# 모름은 선택지가 아니라 '모른다' 응답이다. night는 참거짓이라 문자열 선택지를 둔다
V1_EN_OPTIONS: dict[str, tuple[str, ...]] = {
    **{key: values for key, values in FACT_FIELD_OPTIONS.items() if key in FIELD_OPTIONS},
    "night": ("true", "false"),
}

# 한국어 안에만 있는 칸(확정본에서는 비전 안에서만 쓰고 내보내지 않는다, v1 7절)
_V1_KO_ONLY_KEYS = frozenset({
    "accident_object", "time_condition", "lane_change_A", "lane_change_B",
    "centerline_cross_A", "centerline_cross_B", "collision_part_A", "collision_part_B",
    "motion_trend_A", "motion_trend_B", "weather",
})
# 확정본에서 생긴 칸·값(도표 선택 칸별). 11/6 카드에는 없다
_V1_ONLY_KEYS = frozenset({"relative_position", "collision_time_ms", "narrative"})
# 11/6 도표 선택 값. 지금 `chart_scope`는 v1 값이라, 형식 판정은 이 값과 견준다
# (여기 없는 v1 값이 있으면 확정본 카드다). 나머지 칸은 11/6과 지금 값이 같다
_LEGACY_OPTIONS: dict[str, frozenset[str]] = {
    "road_priority": frozenset({"same_width", "major_minor", "stop_sign"}),
    "movement_A": frozenset({"straight", "left_turn", "right_turn", "u_turn"}),
    "movement_B": frozenset({"straight", "left_turn", "right_turn", "u_turn"}),
    "entry_order": frozenset({"first", "second", "simultaneous"}),
}
_V1_ONLY_VALUES: dict[str, frozenset[str]] = {
    key: frozenset(values) - _LEGACY_OPTIONS.get(key, frozenset(FIELD_OPTIONS[key]))
    for key, values in FACT_FIELD_OPTIONS.items() if key in FIELD_OPTIONS
}

# v1 2절: 도표 A가 카드 B면 맞바꾸는 칸·값
_SWAP_KEYS: dict[str, str] = {
    "movement_A": "movement_B", "movement_B": "movement_A",
    "signal_A": "signal_B", "signal_B": "signal_A",
}
_SWAP_VALUES: dict[str, dict[str, str]] = {
    "road_priority": {
        "major_A": "major_B", "major_B": "major_A", "stop_A": "stop_B", "stop_B": "stop_A",
    },
    "entry_order": {"first": "second", "second": "first"},
    "relative_position": {
        "left": "right", "right": "left",
        "same_direction_front": "same_direction_rear",
        "same_direction_rear": "same_direction_front",
    },
}

_LIGHTS = {"녹색": "green", "황색": "yellow", "적색": "red", "녹색좌회전": "green_left_arrow"}
_MOVES = {"직진": "straight", "좌회전": "left_turn", "우회전": "right_turn", "유턴": "u_turn"}
# 카드 값 → 도표 선택 값(도표 선택 칸 이름 기준). 표에 없는 값은 그대로 둔다
V1_VALUES: dict[str, dict[str, str]] = {
    "accident_type": {"차대차": "car_to_car"},
    "location_type": {
        "사거리_신호있음": "signalized_intersection",
        "사거리_신호없음": "unsignalized_intersection",
        # 한국어 안 4절: 신호 없는 교차로로 묶는 도표는 T자형을 사거리_신호없음과 같이 다룬다.
        # 신호가 확인된 T자형은 신호 교차로로 본다(_value)
        "T자형교차로": "unsignalized_intersection",
        "회전교차로": "roundabout",
        "직선도로": "other", "고속도로": "other", "주차장": "other", "차도와비차도": "other",
        # v1 확정본: 직선도로·고속도로를 따로 내지만 범위 밖 보류는 그대로다(v1 8절)
        "straight_road": "other", "highway": "other",
    },
    "road_priority": {"동일폭": "same_width"},
    "movement_A": _MOVES,
    "movement_B": _MOVES,
    "signal_A": {**_LIGHTS, "신호없음": "no_signal"},
    "signal_B": {**_LIGHTS, "신호없음": "no_signal"},
    "entry_order": {"A먼저": "first", "B먼저": "second", "동시": "simultaneous"},
    "night": {"야간": "true", "주간": "false"},
}
_LIGHT_VALUES = frozenset(_LIGHTS.values())


def card_format(card: FactCard) -> CardFormat:
    """한국어 안 칸·값이 있으면 `v1_ko`, 확정본 칸·값·키가 있으면 `v1`, 아니면 `legacy`.

    `night` 참거짓은 11/6 카드도 같아서 형식을 가르지 않는다.
    """
    fields = card["fields"]
    chart_values = [(chart, field.get("value")) for key, field in fields.items()
                    if (chart := V1_FIELD_KEYS.get(key, key)) in FIELD_OPTIONS]
    if _V1_KO_ONLY_KEYS & fields.keys() or any(
        isinstance(v, str) and v in _V1_VALUE_SET for _, v in chart_values
    ):
        return "v1_ko"
    if (
        _V1_ONLY_KEYS & fields.keys()
        or "parties" in card or "model_info" in card
        or any(isinstance(v, str) and v in _V1_ONLY_VALUES.get(k, ()) for k, v in chart_values)
    ):
        return "v1"
    return "legacy"


def is_v1_card(card: FactCard) -> bool:
    """한국어 안 카드인가. 질문을 한국어 칸 이름·값으로 낸다(`card_field_key` 등)."""
    return card_format(card) == "v1_ko"


def is_swapped(card: FactCard) -> bool:
    """v1 2절: 도표 A가 카드 B(촬영 차량)인가. 그러면 판단은 A/B를 뒤집어 읽는다.

    v1 카드만 본다. 한국어 안·11/6 카드는 카드 A/B를 도표 A/B로 본다.
    """
    if card_format(card) != "v1":
        return False
    mapping = card.get("party_mapping") or {}
    ego = mapping.get("video_ego_party_id")
    return ego is not None and mapping.get("chart_party_A_id") == ego


def chart_key(card_key: str, card: FactCard | None = None) -> str:
    """카드 칸 이름 → 도표 선택 칸 이름. 카드를 주면 v1 2절 맞바꿈도 되돌린다."""
    key = V1_FIELD_KEYS.get(card_key, card_key)
    if card is not None and is_swapped(card):
        key = _SWAP_KEYS.get(key, key)
    return key


def card_field_key(key: str, card: FactCard) -> str:
    """도표 선택 칸 → 질문에 쓸 카드 칸 이름. 답은 이 이름으로 카드 수정에 간다."""
    fmt = card_format(card)
    if fmt == "v1_ko":
        return _CARD_KEYS.get(key, key)
    if fmt == "v1" and is_swapped(card):
        return _SWAP_KEYS.get(key, key)
    return key


def to_card_terms(key: str, value: str, card: FactCard) -> tuple[str, str]:
    """도표 선택 칸·값 → 카드에 쓸 칸·값. 가정 실행이 카드 사본에 값을 넣을 때 쓴다.

    v1 맞바꿈이면 칸(`movement_A` → `movement_B`)과 쪽이 있는 값(`first` → `second`)을 되돌린다.
    그 밖의 형식은 도표 선택 칸 이름 그대로 쓴다(한국어 안 카드에서도 이 이름이 앞선다, `_raw`).
    """
    if not is_swapped(card):
        return key, value
    return _SWAP_KEYS.get(key, key), _SWAP_VALUES.get(key, {}).get(value, value)


def question_options(key: str, card: FactCard) -> list[str]:
    """도표 선택 칸의 질문 선택지. 카드와 같은 형식의 값이다(모름은 '모른다' 응답으로 대신한다).

    v1 맞바꿈에서도 선택지는 카드 쪽 값이다. 도로 관계·진입 순서 선택지는 A/B가 대칭이라 같다.
    """
    fmt = card_format(card)
    if fmt == "v1_ko":
        return [v for v in V1_OPTIONS.get(_CARD_KEYS.get(key, key), ()) if v != UNKNOWN]
    if fmt == "v1":
        return list(V1_EN_OPTIONS.get(key, ()))
    return list(FIELD_OPTIONS.get(key, ()))


def _confirmed(field: Mapping[str, object] | None) -> object:
    if not field or field.get("verification_status") != "human_confirmed":
        return None
    return field.get("value")


def _raw(fields: Mapping[str, FactField], key: str) -> FactField | None:
    """도표 선택 칸의 카드 칸. 이전 이름이 있으면 그것을 쓴다(가정 실행이 이전 이름으로 쓴다)."""
    if key in fields:
        return fields[key]
    alias = _CARD_KEYS.get(key)
    return fields.get(alias) if alias else None


def _value(fields: Mapping[str, FactField], key: str, value: str) -> str:
    """카드 값 하나를 도표 선택 값으로. 대응이 없으면 그대로 둔다."""
    if key == "location_type" and value == "T자형교차로":
        signals = {_value(fields, k, s) for k in ("signal_A", "signal_B")
                   if isinstance(s := _confirmed(_raw(fields, k)), str)}
        return "signalized_intersection" if signals & _LIGHT_VALUES else "unsignalized_intersection"
    if key in ("signal_A", "signal_B") and value == "녹색직진좌회전":
        # 직진·좌회전 동시 신호: 그 차의 진행 방향(확인한 값)으로 정한다. 모르면 그대로 둔다
        movement = _confirmed(_raw(fields, f"movement_{key[-1]}"))
        movement = _MOVES.get(movement, movement) if isinstance(movement, str) else None
        return {"left_turn": "green_left_arrow", "straight": "green"}.get(movement or "", value)
    return V1_VALUES.get(key, {}).get(value, value)


def chart_fields(
    fields: Mapping[str, FactField], *, swapped: bool = False
) -> dict[str, FactField]:
    """카드 칸을 도표 선택 칸 이름·값으로 바꾼 사본. 확인 상태·근거는 그대로 둔다.

    도표 선택 칸(`FIELD_OPTIONS`)만 돌려준다. 확인한 `null`은 `UNKNOWN`이다.
    `해당없음`·`none`·대응 없는 값은 그대로다. `swapped`면 v1 2절대로 A/B를 뒤집어 읽는다.
    """
    out: dict[str, FactField] = {}
    for key in FIELD_OPTIONS:
        field = _raw(fields, _SWAP_KEYS.get(key, key) if swapped else key)
        if field is None:
            continue
        value = field["value"]
        if value is None and field.get("verification_status") == "human_confirmed":
            value = UNKNOWN
        elif isinstance(value, str):
            if swapped:
                value = _SWAP_VALUES.get(key, {}).get(value, value)
            value = _value(fields, key, value)
        out[key] = {**field, "value": value}
    return out


def chart_view(card: FactCard) -> dict[str, FactField]:
    """카드 전체를 도표 선택 칸·값으로(v1 2절 맞바꿈 포함). 판단은 이것으로 카드를 읽는다."""
    return chart_fields(card["fields"], swapped=is_swapped(card))


def confirmed_unknown_fields(card: FactCard) -> list[str]:
    """담당자가 모름으로 확인한 도표 선택 칸. 판단은 이 칸을 '모름' 답처럼 다룬다."""
    return [key for key, field in chart_view(card).items() if _confirmed(field) == UNKNOWN]
