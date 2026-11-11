"""B: 사실 카드 칸·값 → 도표 선택 칸·값(`chart_scope`). 판단 그래프는 카드를 이것으로 읽는다.

A의 카드 v1은 한국어 값을 쓰고 칸 이름 둘이 바뀌었다.
- 문서: 정지원 `docs/contracts/fact-card-v1.md`(11/11 팀 확정 요청, 브랜치 `vision/1111-integrate`)
- 이름: `accident_type` → `accident_object`, `night` → `time_condition`
C의 도표 레코드와 `chart_scope`는 아직 11/6 영어 값이다. 그래서 B가 카드를 읽을 때 바꾼다.
영어 값으로 쓴 카드(이전 형식)는 그대로 통과한다. 도표 레코드를 v1 값으로 바꿀지는 C와 정한다.

- `불명`은 담당자가 확인한 '모름'이다. 다시 묻지 않고, 그 칸으로 갈리는 도표는 고르지 않는다.
- `해당없음`은 값 그대로 둔다. 어떤 도표 값과도 맞지 않고 `*`(무관)과만 맞는다.
- 대응하는 도표 값이 없는 v1 값도 그대로 둔다(예: `진로변경`, `A쪽만_신호등`).
  도표 값과 맞지 않으므로 그 칸으로 갈리는 도표는 빠진다(fail-closed).
- `A대로`·`B쪽_일시정지`처럼 쪽이 붙은 도로 관계도 그대로 둔다. 도표 값 `major_minor`·
  `stop_sign`에는 쪽이 없어서, 대응시키면 A/B가 뒤집힌 도표를 고를 수 있다.
  C 레코드에 쪽을 적는 방법을 정한 뒤 대응한다.
"""

from collections.abc import Mapping

from app.schemas.chart_scope import FIELD_OPTIONS
from app.schemas.facts import FactCard, FactField

UNKNOWN = "불명"
NOT_APPLICABLE = "해당없음"

# 카드 v1 칸 이름 → 도표 선택 칸 이름. 나머지 도표 선택 칸은 이름이 같다
V1_FIELD_KEYS: dict[str, str] = {"accident_object": "accident_type", "time_condition": "night"}
_CARD_KEYS: dict[str, str] = {chart: card for card, chart in V1_FIELD_KEYS.items()}

# 카드 v1 값 목록(A `workers/vision/fields.py`와 같은 순서). 질문 선택지로 쓴다
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
# 도표 선택 칸이 아닌 v1 칸. 하나라도 있으면 v1 카드로 본다
_V1_ONLY_KEYS = frozenset({
    "accident_object", "time_condition", "relative_position", "lane_change_A", "lane_change_B",
    "centerline_cross_A", "centerline_cross_B", "collision_part_A", "collision_part_B",
    "collision_time_ms", "motion_trend_A", "motion_trend_B", "weather", "narrative",
})

_LIGHTS = {"녹색": "green", "황색": "yellow", "적색": "red", "녹색좌회전": "green_left_arrow"}
_MOVES = {"직진": "straight", "좌회전": "left_turn", "우회전": "right_turn", "유턴": "u_turn"}
# 카드 v1 값 → 도표 선택 값(도표 선택 칸 이름 기준). 표에 없는 값은 그대로 둔다
V1_VALUES: dict[str, dict[str, str]] = {
    "accident_type": {"차대차": "car_to_car"},
    "location_type": {
        "사거리_신호있음": "signalized_intersection",
        "사거리_신호없음": "unsignalized_intersection",
        # v1 4절: 신호 없는 교차로로 묶는 도표는 T자형을 사거리_신호없음과 같이 다룬다.
        # 신호가 확인된 T자형은 신호 교차로로 본다(_location)
        "T자형교차로": "unsignalized_intersection",
        "회전교차로": "roundabout",
        "직선도로": "other", "고속도로": "other", "주차장": "other", "차도와비차도": "other",
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


def chart_key(card_key: str) -> str:
    """카드 칸 이름(v1 또는 이전) → 도표 선택 칸 이름."""
    return V1_FIELD_KEYS.get(card_key, card_key)


def is_v1_card(card: FactCard) -> bool:
    """v1 칸이 있거나 도표 선택 칸에 v1 값이 있으면 v1 카드다. 질문을 카드와 같은 형식으로 낸다."""
    fields = card["fields"]
    if _V1_ONLY_KEYS & fields.keys():
        return True
    return any(
        isinstance(field.get("value"), str) and field["value"] in _V1_VALUE_SET
        for key, field in fields.items() if chart_key(key) in FIELD_OPTIONS
    )


def card_field_key(key: str, card: FactCard) -> str:
    """도표 선택 칸 → 질문에 쓸 카드 칸 이름. 답은 이 이름으로 카드 수정에 간다."""
    return _CARD_KEYS.get(key, key) if is_v1_card(card) else key


def question_options(key: str, card: FactCard) -> list[str]:
    """도표 선택 칸의 질문 선택지. v1 카드는 v1 값(`불명`은 '모름' 응답으로 대신한다)."""
    if not is_v1_card(card):
        return list(FIELD_OPTIONS.get(key, ()))
    return [v for v in V1_OPTIONS.get(_CARD_KEYS.get(key, key), ()) if v != UNKNOWN]


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


def chart_fields(fields: Mapping[str, FactField]) -> dict[str, FactField]:
    """카드 칸을 도표 선택 칸 이름·값으로 바꾼 사본. 확인 상태·근거는 그대로 둔다.

    도표 선택 칸(`FIELD_OPTIONS`)만 돌려준다. `불명`·`해당없음`·대응 없는 값은 그대로다.
    """
    out: dict[str, FactField] = {}
    for key in FIELD_OPTIONS:
        field = _raw(fields, key)
        if field is None:
            continue
        value = field["value"]
        if isinstance(value, str):
            value = _value(fields, key, value)
        out[key] = {**field, "value": value}
    return out


def confirmed_unknown_fields(card: FactCard) -> list[str]:
    """담당자가 `불명`으로 확인한 도표 선택 칸. 판단은 이 칸을 '모름' 답처럼 다룬다."""
    return [key for key, field in chart_fields(card["fields"]).items()
            if _confirmed(field) == UNKNOWN]
